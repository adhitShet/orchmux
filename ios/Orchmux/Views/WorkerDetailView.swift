import SwiftUI

struct WorkerDetailView: View {
    let worker: Worker
    @EnvironmentObject var api: APIClient

    @State private var paneText: String = ""
    @State private var paneTimer: Timer?
    @State private var replyText: String = ""
    @State private var force: Bool = false
    @State private var isSending: Bool = false
    @State private var sendError: String?
    @State private var autoScroll: Bool = true
    @State private var selectedTab: DetailTab = .live
    @State private var docs: [APIClient.SessionNote] = []
    @State private var docsLoaded: Bool = false
    @State private var tasks: [APIClient.DispatchHistoryItem] = []
    @State private var tasksLoaded: Bool = false
    @State private var todos: [APIClient.Todo] = []
    @State private var todosLoaded: Bool = false
    @State private var showAddTodo: Bool = false
    @State private var todoError: String?
    @FocusState private var replyFocused: Bool

    enum DetailTab: String, CaseIterable {
        case live  = "Live"
        case tasks = "Tasks"
        case notes = "Notes"
        case docs  = "Docs"
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Picker("", selection: $selectedTab) {
                ForEach(DetailTab.allCases, id: \.self) { t in
                    Text(t.rawValue).tag(t)
                }
            }
            .pickerStyle(.segmented)
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            Divider()
            Group {
                switch selectedTab {
                case .live:  paneScroll
                case .tasks: tasksScroll
                case .notes: todosScroll
                case .docs:  docsScroll
                }
            }
            Divider()
            if selectedTab == .live { replyBar }
        }
        .navigationTitle(worker.session)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button(action: { autoScroll.toggle() }) {
                    Image(systemName: autoScroll ? "arrow.down.to.line" : "arrow.down.to.line.compact")
                        .foregroundStyle(autoScroll ? .blue : .secondary)
                }
            }
        }
        .onAppear { startPanePoll() }
        .onDisappear { stopPanePoll() }
    }

    private var header: some View {
        HStack(spacing: 12) {
            StatusBadge(status: worker.status)
            VStack(alignment: .leading, spacing: 2) {
                Text(worker.domain)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                if let task = worker.currentTask, !task.isEmpty {
                    Text(task)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
            }
            Spacer()
            if let elapsed = worker.elapsedFormatted {
                Text(elapsed)
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.blue)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(Color(.systemGroupedBackground))
    }

    /// Recent tasks dispatched to THIS worker (filtered from /dispatch-history).
    private var tasksScroll: some View {
        Group {
            if tasks.isEmpty {
                VStack(spacing: 10) {
                    Image(systemName: "list.bullet.rectangle")
                        .font(.system(size: 40))
                        .foregroundStyle(.secondary)
                    Text(tasksLoaded ? "No recent tasks" : "Loading…")
                        .foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 10) {
                        ForEach(tasks) { t in
                            TaskHistoryRow(item: t)
                        }
                    }
                    .padding(.horizontal, 14)
                    .padding(.vertical, 12)
                }
                .refreshable { await loadTasks() }
            }
        }
        .task(id: selectedTab) {
            if selectedTab == .tasks && !tasksLoaded { await loadTasks() }
        }
    }

    @MainActor
    private func loadTasks() async {
        do {
            let all = try await api.fetchDispatchHistory(limit: 200)
            tasks = all.filter { $0.session == worker.session }
        } catch {
            tasks = []
        }
        tasksLoaded = true
    }

    /// Docs tab — session-notes markdown files from the Obsidian vault (read-only).
    private var docsScroll: some View {
        Group {
            if docs.isEmpty {
                VStack(spacing: 10) {
                    Image(systemName: "doc.text")
                        .font(.system(size: 40))
                        .foregroundStyle(.secondary)
                    Text(docsLoaded ? "No docs yet" : "Loading…")
                        .foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 12) {
                        ForEach(docs) { note in
                            NoteCard(note: note)
                        }
                    }
                    .padding(.horizontal, 14)
                    .padding(.vertical, 12)
                }
                .refreshable { await loadDocs() }
            }
        }
        .task(id: selectedTab) {
            if selectedTab == .docs && !docsLoaded { await loadDocs() }
        }
    }

    @MainActor
    private func loadDocs() async {
        do {
            docs = try await api.fetchSessionNotes(session: worker.session)
        } catch {
            docs = []
        }
        docsLoaded = true
    }

    /// Notes tab — todos scoped to this worker (via /todos?session=X).
    private var todosScroll: some View {
        Group {
            if todos.isEmpty && todosLoaded {
                ContentUnavailableView {
                    Label("No notes for \(worker.session)", systemImage: "note.text")
                } description: {
                    Text("Tap + to add one.")
                }
            } else {
                List {
                    if let err = todoError {
                        Text(err).foregroundStyle(.red).font(.caption)
                    }
                    if todos.isEmpty && !todosLoaded {
                        HStack { ProgressView(); Text("Loading…").foregroundStyle(.secondary) }
                    }
                    ForEach(todos) { todo in
                        TodoRow(todo: todo,
                                onToggle: { await toggleTodo(todo) },
                                onEdit:   { new in await editTodo(todo, to: new) })
                            .swipeActions {
                                Button(role: .destructive) {
                                    Task { await deleteTodo(todo) }
                                } label: { Label("Delete", systemImage: "trash") }
                            }
                    }
                }
                .listStyle(.plain)
                .refreshable { await loadTodos() }
            }
        }
        .task(id: selectedTab) {
            if selectedTab == .notes && !todosLoaded { await loadTodos() }
        }
        .overlay(alignment: .bottomTrailing) {
            if selectedTab == .notes {
                Button {
                    showAddTodo = true
                } label: {
                    Image(systemName: "plus")
                        .font(.title3.weight(.semibold))
                        .foregroundStyle(.white)
                        .frame(width: 48, height: 48)
                        .background(Color.accentColor, in: Circle())
                        .shadow(radius: 4, y: 2)
                }
                .padding(18)
            }
        }
        .sheet(isPresented: $showAddTodo) {
            TodoEditorSheet(sessions: [worker.session]) { text, _ in
                await createTodo(text: text)
            }
        }
    }

    @MainActor
    private func loadTodos() async {
        do {
            todos = try await api.fetchTodos(session: worker.session)
            todoError = nil
        } catch {
            todoError = error.localizedDescription
            todos = []
        }
        todosLoaded = true
    }

    @MainActor
    private func toggleTodo(_ t: APIClient.Todo) async {
        do {
            try await api.updateTodo(id: t.id, done: !t.done)
            if let idx = todos.firstIndex(where: { $0.id == t.id }) { todos[idx].done.toggle() }
        } catch { todoError = error.localizedDescription }
    }

    @MainActor
    private func editTodo(_ t: APIClient.Todo, to newText: String) async {
        do {
            try await api.updateTodo(id: t.id, text: newText)
            if let idx = todos.firstIndex(where: { $0.id == t.id }) { todos[idx].text = newText }
        } catch { todoError = error.localizedDescription }
    }

    @MainActor
    private func deleteTodo(_ t: APIClient.Todo) async {
        do {
            try await api.deleteTodo(id: t.id)
            todos.removeAll { $0.id == t.id }
        } catch { todoError = error.localizedDescription }
    }

    @MainActor
    private func createTodo(text: String) async {
        do {
            let t = try await api.createTodo(text: text, session: worker.session)
            todos.insert(t, at: 0)
        } catch { todoError = error.localizedDescription }
    }

    private var paneScroll: some View {
        ScrollViewReader { proxy in
            ScrollView {
                Group {
                    if paneText.isEmpty {
                        Text("(no output yet)")
                            .foregroundStyle(.secondary)
                            .italic()
                    } else {
                        PaneNotesView(text: paneText)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
                .id("pane-bottom")
            }
            .onChange(of: paneText) { _, _ in
                guard autoScroll else { return }
                withAnimation(.linear(duration: 0.15)) {
                    proxy.scrollTo("pane-bottom", anchor: .bottom)
                }
            }
        }
    }

    private var replyBar: some View {
        VStack(spacing: 6) {
            if let err = sendError {
                Text(err)
                    .font(.caption2)
                    .foregroundStyle(.red)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            HStack(spacing: 8) {
                TextField("Reply to \(worker.session)…", text: $replyText, axis: .vertical)
                    .lineLimit(1...4)
                    .textFieldStyle(.roundedBorder)
                    .focused($replyFocused)
                Toggle("", isOn: $force)
                    .labelsHidden()
                    .toggleStyle(.button)
                    .tint(.red)
                    .overlay {
                        Image(systemName: "bolt.fill")
                            .font(.caption)
                            .foregroundStyle(force ? .white : .red)
                    }
                    .help("Force (interrupt current task)")
                Button(action: send) {
                    if isSending {
                        ProgressView().scaleEffect(0.7)
                    } else {
                        Image(systemName: "paperplane.fill")
                    }
                }
                .buttonStyle(.borderedProminent)
                .disabled(replyText.trimmingCharacters(in: .whitespaces).isEmpty || isSending)
            }
        }
        .padding(8)
        .background(.bar)
    }

    private func startPanePoll() {
        Task { await loadPane() }
        paneTimer?.invalidate()
        paneTimer = Timer.scheduledTimer(withTimeInterval: 1.5, repeats: true) { _ in
            Task { await loadPane() }
        }
    }

    private func stopPanePoll() {
        paneTimer?.invalidate()
        paneTimer = nil
    }

    @MainActor
    private func loadPane() async {
        do {
            let text = try await api.fetchPane(session: worker.session)
            if text != paneText { paneText = text }
        } catch {
            // silently ignore; keep last text
        }
    }

    private func obsidianURL(for filename: String) -> URL? {
        // Obsidian app deep link: obsidian://open?vault=<vault>&file=<path>
        let vault = "obsidian-vault"
        let path  = "AI-Systems/Claude-Logs/Sessions/\(filename.replacingOccurrences(of: ".md", with: ""))"
        var comp = URLComponents(string: "obsidian://open")
        comp?.queryItems = [
            URLQueryItem(name: "vault", value: vault),
            URLQueryItem(name: "file",  value: path),
        ]
        return comp?.url
    }

    private func send() {
        let msg = replyText.trimmingCharacters(in: .whitespaces)
        guard !msg.isEmpty else { return }
        isSending = true
        sendError = nil
        Task {
            do {
                _ = try await api.dispatch(task: msg, domain: "", session: worker.session, force: force)
                replyText = ""
                replyFocused = false
                await loadPane()
            } catch {
                sendError = "✗ \(error.localizedDescription)"
            }
            isSending = false
        }
    }
}

// MARK: - Note card

private struct NoteCard: View {
    let note: APIClient.SessionNote
    @EnvironmentObject var config: Config
    @State private var expanded: Bool = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Image(systemName: "doc.text.fill")
                    .foregroundStyle(.secondary)
                    .font(.caption)
                if let date = note.date {
                    Text(date)
                        .font(.caption.weight(.semibold))
                }
                Text(note.file.replacingOccurrences(of: ".md", with: ""))
                    .font(.caption2.monospaced())
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Spacer()
                Button {
                    if let url = obsidianURL(for: note.file) {
                        UIApplication.shared.open(url)
                    }
                } label: {
                    Image(systemName: "square.and.pencil")
                        .font(.caption)
                }
                .buttonStyle(.borderless)
                .accessibilityLabel("Open in Obsidian")
            }
            PaneNotesView(text: expanded ? note.preview : truncated(note.preview))
                .padding(.top, 2)
            if note.preview.count > 400 {
                Button(expanded ? "Show less" : "Show more") {
                    expanded.toggle()
                }
                .font(.caption)
            }
        }
        .padding(12)
        .background(Color.secondary.opacity(0.06),
                    in: RoundedRectangle(cornerRadius: 10, style: .continuous))
    }

    private func truncated(_ s: String) -> String {
        if s.count <= 400 { return s }
        return String(s.prefix(400)) + "…"
    }

    private func obsidianURL(for filename: String) -> URL? {
        // Build manually — URLComponents percent-encodes `/` inside query values,
        // and Obsidian's scheme doesn't like that. Only encode spaces/unicode.
        let base = filename.replacingOccurrences(of: ".md", with: "")
        let path = config.obsidianNotesPath.trimmingCharacters(in: CharacterSet(charactersIn: "/")).isEmpty
            ? base
            : "\(config.obsidianNotesPath.trimmingCharacters(in: CharacterSet(charactersIn: "/")))/\(base)"
        let allowed = CharacterSet(charactersIn: "/-_.").union(.alphanumerics)
        let encVault = config.obsidianVault.addingPercentEncoding(withAllowedCharacters: allowed) ?? config.obsidianVault
        let encPath  = path.addingPercentEncoding(withAllowedCharacters: allowed) ?? path
        return URL(string: "obsidian://open?vault=\(encVault)&file=\(encPath)")
    }
}

// MARK: - Task history row (per-worker)

private struct TaskHistoryRow: View {
    let item: APIClient.DispatchHistoryItem

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(item.task)
                .font(.callout)
                .foregroundStyle(.primary)
                .lineLimit(4)
                .textSelection(.enabled)
            HStack(spacing: 6) {
                Text(item.at)
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.secondary)
                if !item.domain.isEmpty {
                    Text("·").foregroundStyle(.tertiary)
                    Text(item.domain)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.secondary.opacity(0.06),
                    in: RoundedRectangle(cornerRadius: 10, style: .continuous))
    }
}
