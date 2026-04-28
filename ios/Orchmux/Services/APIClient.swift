import Foundation
import Combine

/// URLSession delegate that trusts self-signed certs only for Tailscale CGNAT hosts
/// (100.64.0.0/10 — the IP block reserved for Tailscale tailnets). Other hosts go
/// through normal TLS validation.
final class TailnetTrustingDelegate: NSObject, URLSessionDelegate {
    func urlSession(_ session: URLSession,
                    didReceive challenge: URLAuthenticationChallenge,
                    completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              let trust = challenge.protectionSpace.serverTrust else {
            completionHandler(.performDefaultHandling, nil)
            return
        }
        let host = challenge.protectionSpace.host
        if Self.isTailnetHost(host) {
            completionHandler(.useCredential, URLCredential(trust: trust))
        } else {
            completionHandler(.performDefaultHandling, nil)
        }
    }

    /// Tailscale CGNAT range: 100.64.0.0/10  →  100.64.0.0 ... 100.127.255.255
    static func isTailnetHost(_ host: String) -> Bool {
        let parts = host.split(separator: ".").compactMap { Int($0) }
        guard parts.count == 4 else { return false }
        return parts[0] == 100 && parts[1] >= 64 && parts[1] <= 127
    }
}

final class APIClient: ObservableObject {
    static let shared = APIClient()

    @Published var domains:   [DomainStatus] = []
    @Published var questions: [Question]     = []
    @Published var completed: [CompletedTask] = []
    @Published var isConnected: Bool         = false
    @Published var lastError: String?        = nil

    private var timer: AnyCancellable?
    static let urlSession: URLSession = {
        let cfg = URLSessionConfiguration.default
        cfg.timeoutIntervalForRequest = 8
        cfg.waitsForConnectivity = false
        return URLSession(configuration: cfg, delegate: TailnetTrustingDelegate(), delegateQueue: nil)
    }()
    private var session: URLSession { Self.urlSession }
    private var config: Config { Config.shared }

    private init() {}

    // MARK: - Polling

    func startPolling() {
        refresh()
        timer = Timer.publish(every: 3, on: .main, in: .common)
            .autoconnect()
            .sink { [weak self] _ in self?.refresh() }
    }

    func stopPolling() {
        timer?.cancel()
    }

    func refresh() {
        Task {
            await fetchStatus()
            await fetchQuestions()
        }
    }

    // MARK: - Status

    @MainActor
    func fetchStatus() async {
        guard let url = URL(string: "\(config.serverURL)/status") else { return }
        do {
            let (data, _) = try await session.data(from: url)
            let raw = try JSONDecoder().decode([String: RawDomain].self, from: data)
            domains = raw.map { domain, d in
                let workers = d.workers.map { w in
                    Worker(
                        id: w.session,
                        session: w.session,
                        domain: domain,
                        status: Worker.WorkerStatus(rawValue: w.status) ?? .unknown,
                        currentTask: w.current_task,
                        elapsedSeconds: w.elapsed_seconds,
                        paneProgress: w.pane_progress,
                        workerType: w.worker_type ?? "persistent"
                    )
                }
                return DomainStatus(id: domain, workers: workers, queueDepth: d.queue_depth, model: d.model ?? "claude")
            }.sorted { $0.id < $1.id }
            isConnected = true
            lastError = nil
        } catch {
            isConnected = false
            lastError = error.localizedDescription
        }
    }

    // MARK: - Questions

    @MainActor
    func fetchQuestions() async {
        guard let url = URL(string: "\(config.serverURL)/questions") else { return }
        do {
            let (data, _) = try await session.data(from: url)
            let raw = try JSONDecoder().decode(RawQuestions.self, from: data)
            questions = raw.pending.map {
                Question(id: $0.id, message: $0.message, session: $0.session ?? "",
                         askedAt: $0.asked_at ?? "", answered: $0.answered ?? false)
            }
        } catch {}
    }

    @MainActor
    func answerQuestion(id: String, answer: String) async throws {
        guard let url = URL(string: "\(config.serverURL)/answer/\(id)") else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: ["answer": answer])
        _ = try await session.data(for: req)
        questions.removeAll { $0.id == id }
    }

    @MainActor
    func dismissQuestion(id: String) async {
        guard let url = URL(string: "\(config.serverURL)/questions/\(id)") else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "DELETE"
        _ = try? await session.data(for: req)
        questions.removeAll { $0.id == id }
    }

    // MARK: - Dispatch

    struct DispatchResult {
        let taskId: String
        let session: String
        let status: String
    }

    struct ServerError: LocalizedError {
        let status: Int
        let detail: String
        var errorDescription: String? { "\(status): \(detail)" }
    }

    @MainActor
    func dispatch(task: String, domain: String, session: String? = nil, force: Bool = false) async throws -> DispatchResult {
        guard let url = URL(string: "\(config.serverURL)/dispatch") else {
            throw URLError(.badURL)
        }
        var body: [String: Any] = ["task": task, "domain": domain, "force": force]
        if let s = session { body["session"] = s }

        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: body)

        let (data, response) = try await self.session.data(for: req)
        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            // FastAPI returns {"detail":"..."} on errors
            let detail = (try? JSONDecoder().decode(RawError.self, from: data))?.detail
                ?? String(data: data, encoding: .utf8)
                ?? "HTTP \(http.statusCode)"
            throw ServerError(status: http.statusCode, detail: detail)
        }
        let raw = try JSONDecoder().decode(RawDispatchResponse.self, from: data)
        return DispatchResult(taskId: raw.task_id ?? "", session: raw.session ?? "", status: raw.status ?? "")
    }

    private struct RawError: Decodable { let detail: String }

    // MARK: - Pane (live tmux output)

    @MainActor
    func fetchPane(session sessionName: String) async throws -> String {
        guard let url = URL(string: "\(config.serverURL)/pane/\(sessionName)") else {
            throw URLError(.badURL)
        }
        let (data, _) = try await session.data(from: url)
        let raw = try JSONDecoder().decode(RawPane.self, from: data)
        return PaneProcessor.clean(raw.output ?? "")
    }

    private struct RawPane: Decodable {
        let session: String?
        let output: String?
        let exists: Bool?
    }

    // MARK: - Session notes (docs per worker, date-wise)

    struct SessionNote: Identifiable {
        let id: String          // filename
        let file: String
        let preview: String
        var date: String? {     // extract YYYY-MM-DD prefix from filename
            guard file.count >= 10 else { return nil }
            let p = String(file.prefix(10))
            return p.contains("-") ? p : nil
        }
    }

    @MainActor
    func fetchSessionNotes(session sessionName: String) async throws -> [SessionNote] {
        guard let url = URL(string: "\(config.serverURL)/session-notes/\(sessionName)") else {
            throw URLError(.badURL)
        }
        var req = URLRequest(url: url)
        if !config.vaultToken.isEmpty {
            req.setValue("Bearer \(config.vaultToken)", forHTTPHeaderField: "Authorization")
        }
        let (data, _) = try await session.data(for: req)
        let raw = try JSONDecoder().decode(RawSessionNotes.self, from: data)
        return (raw.notes ?? []).map { SessionNote(id: $0.file, file: $0.file, preview: $0.preview ?? "") }
    }

    private struct RawSessionNotes: Decodable {
        let session: String?
        let notes: [RawSessionNote]?
    }

    private struct RawSessionNote: Decodable {
        let file: String
        let preview: String?
    }

    // MARK: - Dispatch history

    struct DispatchHistoryItem: Identifiable {
        let id: String
        let task: String
        let domain: String
        let session: String
        let at: String
    }

    @MainActor
    func fetchDispatchHistory(limit: Int = 40) async throws -> [DispatchHistoryItem] {
        guard var comp = URLComponents(string: "\(config.serverURL)/dispatch-history") else {
            throw URLError(.badURL)
        }
        if !config.vaultToken.isEmpty {
            comp.queryItems = [URLQueryItem(name: "token", value: config.vaultToken)]
        }
        guard let url = comp.url else { throw URLError(.badURL) }

        let (data, response) = try await session.data(from: url)
        if let http = response as? HTTPURLResponse, http.statusCode == 403 {
            throw ServerError(status: 403, detail: "Dispatch history is token-protected; set Vault Token in Settings.")
        }
        let raw = try JSONDecoder().decode([RawHistory].self, from: data)
        let items = raw.prefix(limit).enumerated().map { idx, h in
            DispatchHistoryItem(
                id: "\(idx)-\(h.at ?? "")-\(h.session ?? "")",
                task:    h.task ?? "",
                domain:  h.domain ?? "",
                session: h.session ?? "",
                at:      h.at ?? ""
            )
        }
        return items
    }

    private struct RawHistory: Decodable {
        let task: String?
        let domain: String?
        let session: String?
        let at: String?
    }

    // MARK: - Vault info (server tells us where notes live)

    struct VaultInfo: Decodable {
        let vault_name: String
        let vault_path: String?
        let notes_path: String?
    }

    @MainActor
    func fetchVaultInfo() async throws -> VaultInfo {
        guard let url = URL(string: "\(config.serverURL)/vault-info") else {
            throw URLError(.badURL)
        }
        let (data, response) = try await session.data(from: url)
        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            throw ServerError(status: http.statusCode, detail: "vault-info unavailable")
        }
        return try JSONDecoder().decode(VaultInfo.self, from: data)
    }

    // MARK: - Todos (global + per-session notes)

    struct Todo: Identifiable, Equatable {
        let id: Int64
        var text: String
        var done: Bool
        var session: String?        // nil / empty = global
    }

    /// - Parameter session: `nil` → all todos, `""` → only global, non-empty → that session's todos
    @MainActor
    func fetchTodos(session sessionFilter: String? = nil) async throws -> [Todo] {
        guard var comp = URLComponents(string: "\(config.serverURL)/todos") else {
            throw URLError(.badURL)
        }
        if let s = sessionFilter {
            comp.queryItems = [URLQueryItem(name: "session", value: s)]
        }
        guard let url = comp.url else { throw URLError(.badURL) }
        let (data, response) = try await session.data(from: url)
        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            let detail = (try? JSONDecoder().decode(RawError.self, from: data))?.detail ?? "HTTP \(http.statusCode)"
            throw ServerError(status: http.statusCode, detail: detail)
        }
        let raw = try JSONDecoder().decode([RawTodo].self, from: data)
        return raw.map {
            Todo(id: $0.id, text: $0.text ?? "", done: $0.done ?? false, session: $0.session)
        }
    }

    @MainActor
    @discardableResult
    func createTodo(text: String, session: String? = nil) async throws -> Todo {
        guard let url = URL(string: "\(config.serverURL)/todos") else { throw URLError(.badURL) }
        var body: [String: Any] = ["text": text, "done": false]
        if let s = session, !s.isEmpty { body["session"] = s }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (data, response) = try await self.session.data(for: req)
        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            let detail = (try? JSONDecoder().decode(RawError.self, from: data))?.detail ?? "HTTP \(http.statusCode)"
            throw ServerError(status: http.statusCode, detail: detail)
        }
        let raw = try JSONDecoder().decode(RawTodoCreateResponse.self, from: data)
        return Todo(id: raw.id ?? Int64(Date().timeIntervalSince1970 * 1000),
                    text: text, done: false, session: session)
    }

    @MainActor
    func updateTodo(id: Int64, text: String? = nil, done: Bool? = nil, session: String? = nil) async throws {
        guard let url = URL(string: "\(config.serverURL)/todos/\(id)") else { throw URLError(.badURL) }
        var body: [String: Any] = [:]
        if let t = text    { body["text"]    = t }
        if let d = done    { body["done"]    = d }
        if let s = session { body["session"] = s.isEmpty ? NSNull() : s }
        var req = URLRequest(url: url)
        req.httpMethod = "PATCH"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (data, response) = try await self.session.data(for: req)
        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            let detail = (try? JSONDecoder().decode(RawError.self, from: data))?.detail ?? "HTTP \(http.statusCode)"
            throw ServerError(status: http.statusCode, detail: detail)
        }
    }

    @MainActor
    func deleteTodo(id: Int64) async throws {
        guard let url = URL(string: "\(config.serverURL)/todos/\(id)") else { throw URLError(.badURL) }
        var req = URLRequest(url: url)
        req.httpMethod = "DELETE"
        let (data, response) = try await self.session.data(for: req)
        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            let detail = (try? JSONDecoder().decode(RawError.self, from: data))?.detail ?? "HTTP \(http.statusCode)"
            throw ServerError(status: http.statusCode, detail: detail)
        }
    }

    private struct RawTodo: Decodable {
        let id: Int64
        let text: String?
        let done: Bool?
        let session: String?
    }

    private struct RawTodoCreateResponse: Decodable {
        let ok: Bool?
        let id: Int64?
    }

    // MARK: - Task Detail

    @MainActor
    func fetchTask(taskId: String) async throws -> TaskDetail {
        guard let url = URL(string: "\(config.serverURL)/task/\(taskId)") else {
            throw URLError(.badURL)
        }
        let (data, _) = try await session.data(from: url)
        let raw = try JSONDecoder().decode(RawTask.self, from: data)
        return TaskDetail(id: raw.task_id ?? taskId, task: raw.task ?? "",
                          domain: raw.domain ?? "", session: raw.session ?? "",
                          status: raw.status ?? "", result: raw.result)
    }

    // MARK: - Completed

    @MainActor
    func fetchCompleted() async {
        guard let url = URL(string: "\(config.serverURL)/completed") else { return }
        do {
            let (data, _) = try await session.data(from: url)
            let raw = try JSONDecoder().decode([RawCompleted].self, from: data)
            completed = raw.map {
                CompletedTask(id: $0.task_id ?? UUID().uuidString,
                              session: $0.session ?? "",
                              result: $0.result ?? "",
                              success: $0.success ?? true)
            }
        } catch {}
    }

    // MARK: - Raw Decodable models

    private struct RawDomain: Decodable {
        let workers: [RawWorker]
        let queue_depth: Int
        let model: String?
    }

    private struct RawWorker: Decodable {
        let session: String
        let status: String
        let current_task: String?
        let elapsed_seconds: Int?
        let pane_progress: String?
        let worker_type: String?
    }

    private struct RawQuestions: Decodable {
        let pending: [RawQuestion]
    }

    private struct RawQuestion: Decodable {
        let id: String
        let message: String
        let session: String?
        let asked_at: String?
        let answered: Bool?
    }

    private struct RawDispatchResponse: Decodable {
        let task_id: String?
        let session: String?
        let status: String?
    }

    private struct RawTask: Decodable {
        let task_id: String?
        let task: String?
        let domain: String?
        let session: String?
        let status: String?
        let result: String?
    }

    private struct RawCompleted: Decodable {
        let task_id: String?
        let session: String?
        let result: String?
        let success: Bool?
    }
}
