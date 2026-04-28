import SwiftUI

/// App-level Notes / Todos view. Consolidates `GET /todos` — shows everything
/// across workers with session attribution, lets you add/edit/complete/delete.
///
/// Filter menu: All · Global only · By session
struct TodosView: View {
    @EnvironmentObject var api: APIClient

    @State private var todos: [APIClient.Todo] = []
    @State private var loading: Bool = false
    @State private var error: String?
    @State private var search: String = ""
    @State private var filter: Filter = .all
    @State private var showAdd: Bool = false

    enum Filter: Hashable {
        case all
        case globalOnly
        case session(String)
        var label: String {
            switch self {
            case .all:          return "All"
            case .globalOnly:   return "Global"
            case .session(let s): return s
            }
        }
    }

    private var filteredTodos: [APIClient.Todo] {
        let base: [APIClient.Todo]
        switch filter {
        case .all:
            base = todos
        case .globalOnly:
            base = todos.filter { ($0.session ?? "").isEmpty }
        case .session(let s):
            base = todos.filter { $0.session == s }
        }
        let q = search.trimmingCharacters(in: .whitespaces).lowercased()
        guard !q.isEmpty else { return base }
        return base.filter { $0.text.lowercased().contains(q) }
    }

    /// Grouped: Global at top, then each session alphabetical.
    private var grouped: [(title: String, todos: [APIClient.Todo])] {
        let groups = Dictionary(grouping: filteredTodos) { ($0.session ?? "").isEmpty ? "__global__" : $0.session! }
        let sessions = groups.keys.filter { $0 != "__global__" }.sorted()
        var out: [(String, [APIClient.Todo])] = []
        if let g = groups["__global__"], !g.isEmpty {
            out.append(("Global", sortTodos(g)))
        }
        for s in sessions {
            if let t = groups[s] { out.append((s, sortTodos(t))) }
        }
        return out
    }

    private func sortTodos(_ t: [APIClient.Todo]) -> [APIClient.Todo] {
        // Open first, then done, newer (higher id) first within each bucket.
        t.sorted { a, b in
            if a.done != b.done { return !a.done && b.done }
            return a.id > b.id
        }
    }

    private var sessionsPresent: [String] {
        let s = Set(todos.compactMap { $0.session }).filter { !$0.isEmpty }
        return Array(s).sorted()
    }

    private var openCount: Int { todos.filter { !$0.done }.count }

    var body: some View {
        NavigationStack {
            Group {
                if todos.isEmpty && !loading {
                    ContentUnavailableView {
                        Label("No notes yet", systemImage: "note.text")
                    } description: {
                        Text("Tap + to jot a todo or reminder.")
                    }
                } else {
                    list
                }
            }
            .navigationTitle("Notes")
            .navigationBarTitleDisplayMode(.large)
            .searchable(text: $search, prompt: "Search notes")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) { filterMenu }
                ToolbarItem(placement: .topBarTrailing) {
                    Button { showAdd = true } label: {
                        Image(systemName: "plus.circle.fill")
                    }
                }
            }
            .task { await load() }
            .refreshable { await load() }
            .sheet(isPresented: $showAdd) {
                TodoEditorSheet(sessions: sessionsPresent) { text, session in
                    await createTodo(text: text, session: session)
                }
            }
            .overlay(alignment: .bottom) {
                if let err = error {
                    Text(err)
                        .font(.caption)
                        .foregroundStyle(.red)
                        .padding(8)
                        .background(.regularMaterial, in: Capsule())
                        .padding(.bottom, 12)
                }
            }
        }
    }

    // MARK: subviews

    private var filterMenu: some View {
        Menu {
            Picker("Filter", selection: $filter) {
                Text("All (\(todos.count))").tag(Filter.all)
                Text("Global (\(todos.filter { ($0.session ?? "").isEmpty }.count))")
                    .tag(Filter.globalOnly)
                Divider()
                ForEach(sessionsPresent, id: \.self) { s in
                    let n = todos.filter { $0.session == s }.count
                    Text("\(s) (\(n))").tag(Filter.session(s))
                }
            }
        } label: {
            HStack(spacing: 2) {
                Image(systemName: "line.3.horizontal.decrease.circle")
                Text(filter.label).font(.caption)
            }
        }
    }

    private var list: some View {
        List {
            if loading && todos.isEmpty {
                HStack { ProgressView(); Text("Loading…").foregroundStyle(.secondary) }
            }
            ForEach(grouped, id: \.title) { group in
                Section {
                    ForEach(group.todos) { todo in
                        TodoRow(todo: todo,
                                onToggle:  { await toggle(todo) },
                                onEdit:    { newText in await editText(todo, to: newText) })
                            .swipeActions {
                                Button(role: .destructive) {
                                    Task { await delete(todo) }
                                } label: {
                                    Label("Delete", systemImage: "trash")
                                }
                            }
                    }
                } header: {
                    HStack(spacing: 6) {
                        if group.title == "Global" {
                            Image(systemName: "globe").font(.caption).foregroundStyle(.secondary)
                        } else {
                            Image(systemName: "cpu").font(.caption).foregroundStyle(.secondary)
                        }
                        Text(group.title).font(.caption.weight(.semibold))
                        Text("\(group.todos.count)").font(.caption.monospacedDigit()).foregroundStyle(.tertiary)
                    }
                }
            }
        }
        .listStyle(.insetGrouped)
    }

    // MARK: actions

    @MainActor
    private func load() async {
        loading = true
        defer { loading = false }
        do {
            todos = try await api.fetchTodos(session: nil)
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    @MainActor
    private func toggle(_ todo: APIClient.Todo) async {
        do {
            try await api.updateTodo(id: todo.id, done: !todo.done)
            if let idx = todos.firstIndex(where: { $0.id == todo.id }) {
                todos[idx].done.toggle()
            }
        } catch {
            self.error = error.localizedDescription
        }
    }

    @MainActor
    private func editText(_ todo: APIClient.Todo, to newText: String) async {
        do {
            try await api.updateTodo(id: todo.id, text: newText)
            if let idx = todos.firstIndex(where: { $0.id == todo.id }) {
                todos[idx].text = newText
            }
        } catch {
            self.error = error.localizedDescription
        }
    }

    @MainActor
    private func delete(_ todo: APIClient.Todo) async {
        do {
            try await api.deleteTodo(id: todo.id)
            todos.removeAll { $0.id == todo.id }
        } catch {
            self.error = error.localizedDescription
        }
    }

    @MainActor
    private func createTodo(text: String, session: String?) async {
        do {
            let t = try await api.createTodo(text: text, session: session)
            todos.insert(t, at: 0)
        } catch {
            self.error = error.localizedDescription
        }
    }
}

// MARK: - Todo row

struct TodoRow: View {
    let todo: APIClient.Todo
    let onToggle: () async -> Void
    let onEdit: (String) async -> Void

    @State private var editing: Bool = false
    @State private var draft: String = ""

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Button {
                Task { await onToggle() }
            } label: {
                Image(systemName: todo.done ? "checkmark.circle.fill" : "circle")
                    .font(.title3)
                    .foregroundStyle(todo.done ? Color.accentColor : .secondary)
            }
            .buttonStyle(.plain)

            if editing {
                VStack(alignment: .leading, spacing: 6) {
                    TextField("Note", text: $draft, axis: .vertical)
                        .textFieldStyle(.roundedBorder)
                        .lineLimit(1...6)
                    HStack {
                        Button("Cancel") { editing = false }
                            .buttonStyle(.bordered)
                            .controlSize(.small)
                        Button("Save") {
                            let trimmed = draft.trimmingCharacters(in: .whitespacesAndNewlines)
                            Task {
                                if !trimmed.isEmpty && trimmed != todo.text {
                                    await onEdit(trimmed)
                                }
                                editing = false
                            }
                        }
                        .buttonStyle(.borderedProminent)
                        .controlSize(.small)
                    }
                }
            } else {
                Text(todo.text)
                    .font(.callout)
                    .strikethrough(todo.done, color: .secondary)
                    .foregroundStyle(todo.done ? .secondary : .primary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .textSelection(.enabled)
                    .onLongPressGesture {
                        draft = todo.text
                        editing = true
                    }
            }
        }
        .padding(.vertical, 4)
    }
}

// MARK: - Editor sheet

struct TodoEditorSheet: View {
    let sessions: [String]
    let onSave: (String, String?) async -> Void
    @Environment(\.dismiss) private var dismiss

    @State private var text: String = ""
    @State private var session: String = ""
    @State private var saving: Bool = false

    var body: some View {
        NavigationStack {
            Form {
                Section("Note") {
                    TextField("What's on your mind?", text: $text, axis: .vertical)
                        .lineLimit(3...8)
                }
                Section {
                    Picker("Attach to", selection: $session) {
                        HStack { Image(systemName: "globe"); Text("Global") }.tag("")
                        ForEach(sessions, id: \.self) { s in
                            Text(s).tag(s)
                        }
                    }
                } header: {
                    Text("Scope")
                } footer: {
                    Text(session.isEmpty
                         ? "Global notes show on the app-wide Notes tab only."
                         : "Will also show on \(session)'s worker view.")
                }
            }
            .navigationTitle("New note")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") {
                        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
                        guard !trimmed.isEmpty else { return }
                        saving = true
                        Task {
                            await onSave(trimmed, session.isEmpty ? nil : session)
                            dismiss()
                        }
                    }
                    .disabled(text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || saving)
                }
            }
        }
    }
}
