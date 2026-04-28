import SwiftUI

struct DispatchView: View {
    @EnvironmentObject var api: APIClient
    @State private var taskText = ""
    @State private var selectedDomain = "(auto)"
    @State private var specificSession = ""
    @State private var force: Bool = false
    @State private var isDispatching = false
    @State private var lastResult: Result?
    @FocusState private var taskFocused: Bool
    @State private var showAlert: Bool = false
    @State private var alertMessage: String = ""
    @State private var history: [APIClient.DispatchHistoryItem] = []
    @State private var historyLoading: Bool = false
    @State private var historyError: String?

    enum Result {
        case ok(taskId: String, session: String, status: String)
        case fail(message: String)
    }

    private var allWorkers: [Worker] { api.domains.flatMap(\.workers) }

    private var domains: [String] {
        ["(auto)"] + api.domains.map(\.id)
    }

    /// Domain inferred from the picked worker — keeps Dispatch valid when user
    /// only picks a session and leaves Domain on (auto).
    private var inferredDomain: String? {
        guard !specificSession.isEmpty else { return nil }
        return allWorkers.first(where: { $0.session == specificSession })?.domain
    }

    /// True when payload would be `{domain:"", session:nil}` — server returns 404.
    private var isInvalid: Bool {
        let domain = selectedDomain == "(auto)" ? (inferredDomain ?? "") : selectedDomain
        return domain.isEmpty && specificSession.isEmpty
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("Task") {
                    TextEditor(text: $taskText)
                        .frame(minHeight: 120)
                        .focused($taskFocused)
                }

                Section {
                    Picker("Domain", selection: $selectedDomain) {
                        ForEach(domains, id: \.self) { d in
                            Text(d).tag(d)
                        }
                    }

                    if !allWorkers.isEmpty {
                        NavigationLink {
                            WorkerPickerSheet(
                                workers: allWorkers,
                                selected: $specificSession
                            )
                        } label: {
                            HStack {
                                Text("Worker")
                                Spacer()
                                if specificSession.isEmpty {
                                    Text("Any in domain")
                                        .foregroundStyle(.secondary)
                                } else {
                                    HStack(spacing: 4) {
                                        Text(specificSession)
                                            .font(.callout.monospaced())
                                        if let w = allWorkers.first(where: { $0.session == specificSession }) {
                                            Text("·").foregroundStyle(.tertiary)
                                            Text(w.domain)
                                                .font(.caption)
                                                .foregroundStyle(.secondary)
                                        }
                                    }
                                    .foregroundStyle(.secondary)
                                }
                            }
                        }
                    }
                } header: {
                    Text("Routing")
                } footer: {
                    if let inferred = inferredDomain, selectedDomain == "(auto)" {
                        Text("Auto-routing to **\(inferred)** domain (worker’s home)").font(.caption)
                    } else if isInvalid {
                        Text("Pick a domain or a worker — both can’t be Auto.")
                            .foregroundStyle(.orange)
                            .font(.caption)
                    }
                }

                Section {
                    Toggle(isOn: $force) {
                        Label {
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Force")
                                Text("Interrupts the worker’s current task")
                                    .font(.caption2)
                                    .foregroundStyle(.secondary)
                            }
                        } icon: {
                            Image(systemName: "bolt.fill")
                                .foregroundStyle(force ? .red : .secondary)
                        }
                    }
                    .disabled(specificSession.isEmpty)
                }

                Section {
                    Button(action: dispatch) {
                        if isDispatching {
                            HStack {
                                ProgressView().scaleEffect(0.8)
                                Text("Dispatching…")
                            }
                        } else {
                            Label(force ? "Force Dispatch" : "Dispatch",
                                  systemImage: force ? "bolt.fill" : "paperplane.fill")
                                .foregroundStyle(force ? .red : .blue)
                        }
                    }
                    .disabled(taskText.trimmingCharacters(in: .whitespaces).isEmpty
                              || isDispatching
                              || isInvalid)
                }

                Section {
                    if historyLoading && history.isEmpty {
                        HStack(spacing: 8) {
                            ProgressView().scaleEffect(0.8)
                            Text("Loading history…").foregroundStyle(.secondary)
                        }
                    } else if let err = historyError, history.isEmpty {
                        Text(err).font(.caption).foregroundStyle(.orange)
                    } else if history.isEmpty {
                        Text("No recent dispatches").font(.caption).foregroundStyle(.secondary)
                    } else {
                        ForEach(history) { item in
                            HistoryRow(item: item) {
                                applyHistory(item)
                            }
                        }
                    }
                } header: {
                    HStack {
                        Text("Recent")
                        Spacer()
                        Button {
                            Task { await loadHistory() }
                        } label: {
                            Image(systemName: "arrow.clockwise")
                                .font(.caption)
                        }
                        .buttonStyle(.borderless)
                    }
                }

                if let result = lastResult {
                    Section("Last Result") {
                        switch result {
                        case .ok(let id, let session, let status):
                            Label {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text("\(session) — \(status)")
                                        .font(.callout.weight(.semibold))
                                    Text(id)
                                        .font(.caption2.monospaced())
                                        .foregroundStyle(.secondary)
                                }
                            } icon: {
                                Image(systemName: "checkmark.circle.fill")
                                    .foregroundStyle(.green)
                            }
                        case .fail(let m):
                            Label {
                                Text(m).font(.callout)
                            } icon: {
                                Image(systemName: "xmark.octagon.fill")
                                    .foregroundStyle(.red)
                            }
                        }
                    }
                }
            }
            .navigationTitle("Dispatch")
            .alert("Dispatch failed", isPresented: $showAlert) {
                Button("OK", role: .cancel) {}
            } message: {
                Text(alertMessage)
            }
            .task { await loadHistory() }
            .refreshable { await loadHistory() }
        }
    }

    @MainActor
    private func loadHistory() async {
        historyLoading = true
        defer { historyLoading = false }
        do {
            history = try await api.fetchDispatchHistory(limit: 40)
            historyError = nil
        } catch {
            historyError = error.localizedDescription
        }
    }

    private func applyHistory(_ item: APIClient.DispatchHistoryItem) {
        taskText = item.task
        specificSession = item.session
        selectedDomain = item.domain.isEmpty ? "(auto)" : item.domain
        taskFocused = true
    }

    private func dispatch() {
        let task = taskText.trimmingCharacters(in: .whitespaces)
        guard !task.isEmpty else { return }
        guard !isInvalid else {
            alertMessage = "Pick a domain or a worker — both can’t be Auto."
            showAlert = true
            return
        }
        isDispatching = true
        taskFocused = false

        // Resolve domain: user choice wins, else inferred from session, else empty (server will 404).
        let domain: String = {
            if selectedDomain != "(auto)" { return selectedDomain }
            return inferredDomain ?? ""
        }()
        let session = specificSession.isEmpty ? nil : specificSession
        let useForce = force

        Task {
            do {
                let r = try await api.dispatch(task: task, domain: domain, session: session, force: useForce)
                lastResult = .ok(taskId: r.taskId, session: r.session, status: r.status)
                taskText = ""
                specificSession = ""
                force = false
                await loadHistory()
            } catch {
                let msg = error.localizedDescription
                lastResult = .fail(message: msg)
                alertMessage = msg
                showAlert = true
            }
            isDispatching = false
        }
    }
}

// MARK: - History row

private struct HistoryRow: View {
    let item: APIClient.DispatchHistoryItem
    let onTap: () -> Void

    var body: some View {
        Button(action: onTap) {
            VStack(alignment: .leading, spacing: 3) {
                Text(item.task)
                    .font(.callout)
                    .foregroundStyle(.primary)
                    .lineLimit(2)
                HStack(spacing: 6) {
                    Text(item.session)
                        .font(.caption2.monospaced())
                        .foregroundStyle(.secondary)
                    if !item.domain.isEmpty {
                        Text("·").foregroundStyle(.tertiary)
                        Text(item.domain)
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    Text(item.at)
                        .font(.caption2.monospacedDigit())
                        .foregroundStyle(.tertiary)
                }
            }
        }
        .buttonStyle(.plain)
    }
}

// MARK: - Worker picker (pushed screen, searchable, grouped by domain)

private struct WorkerPickerSheet: View {
    let workers: [Worker]
    @Binding var selected: String
    @Environment(\.dismiss) private var dismiss
    @State private var search: String = ""

    private var filtered: [Worker] {
        let q = search.trimmingCharacters(in: .whitespaces).lowercased()
        guard !q.isEmpty else { return workers }
        return workers.filter {
            $0.session.lowercased().contains(q) ||
            $0.domain.lowercased().contains(q) ||
            ($0.currentTask ?? "").lowercased().contains(q)
        }
    }

    /// Group by domain, preserving a stable domain order (by count desc, then a-z).
    private var grouped: [(domain: String, workers: [Worker])] {
        let dict = Dictionary(grouping: filtered, by: \.domain)
        let sorted = dict.sorted { a, b in
            if a.value.count != b.value.count { return a.value.count > b.value.count }
            return a.key < b.key
        }
        return sorted.map { (domain: $0.key, workers: $0.value.sorted { $0.session < $1.session }) }
    }

    var body: some View {
        List {
            // "Any in domain" row at top — represents no specific worker
            Section {
                Button {
                    selected = ""
                    dismiss()
                } label: {
                    HStack {
                        Text("Any in domain")
                            .foregroundStyle(.primary)
                        Spacer()
                        if selected.isEmpty {
                            Image(systemName: "checkmark")
                                .foregroundStyle(.blue)
                        }
                    }
                }
            }

            ForEach(grouped, id: \.domain) { group in
                Section {
                    ForEach(group.workers) { w in
                        Button {
                            selected = w.session
                            dismiss()
                        } label: {
                            HStack(spacing: 10) {
                                StatusIndicator(status: w.status)
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(w.session)
                                        .font(.system(size: 14, design: .monospaced))
                                        .foregroundStyle(.primary)
                                    if let task = w.currentTask, !task.isEmpty {
                                        Text(task)
                                            .font(.caption2)
                                            .foregroundStyle(.secondary)
                                            .lineLimit(1)
                                    }
                                }
                                Spacer()
                                if selected == w.session {
                                    Image(systemName: "checkmark")
                                        .foregroundStyle(.blue)
                                }
                            }
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                    }
                } header: {
                    HStack(spacing: 6) {
                        Text(group.domain.uppercased())
                        Text("\(group.workers.count)")
                            .foregroundStyle(.tertiary)
                    }
                }
            }
        }
        .listStyle(.insetGrouped)
        .searchable(text: $search, placement: .navigationBarDrawer(displayMode: .always),
                    prompt: "Search workers")
        .navigationTitle("Pick worker")
        .navigationBarTitleDisplayMode(.inline)
    }
}
