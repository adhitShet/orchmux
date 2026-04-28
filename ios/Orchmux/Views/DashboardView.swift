import SwiftUI

// MARK: - Dashboard

struct DashboardView: View {
    @EnvironmentObject var api: APIClient

    @State private var search: String = ""
    @State private var sortBy: SortKey = .recent
    @State private var collapsedSections: Set<Worker.WorkerStatus> = [.idle, .missing, .offline]

    enum SortKey: String, CaseIterable, Identifiable {
        case recent = "Recent"
        case oldest = "Oldest"
        case alpha  = "A–Z"
        var id: String { rawValue }
        var symbol: String {
            switch self {
            case .recent: return "clock.arrow.2.circlepath"
            case .oldest: return "clock.arrow.circlepath"
            case .alpha:  return "textformat.abc"
            }
        }
    }

    private var allWorkers: [Worker] {
        api.domains.flatMap(\.workers)
    }

    private var filtered: [Worker] {
        let q = search.trimmingCharacters(in: .whitespaces).lowercased()
        guard !q.isEmpty else { return allWorkers }
        return allWorkers.filter {
            $0.session.lowercased().contains(q)
                || $0.domain.lowercased().contains(q)
                || ($0.currentTask ?? "").lowercased().contains(q)
        }
    }

    private var groupedByStatus: [(Worker.WorkerStatus, [Worker])] {
        let order: [Worker.WorkerStatus] = [.busy, .waiting, .blocked, .idle, .missing, .offline]
        return order.compactMap { status in
            let bucket = filtered.filter { $0.status == status }
            guard !bucket.isEmpty else { return nil }
            return (status, sort(bucket))
        }
    }

    private func sort(_ bucket: [Worker]) -> [Worker] {
        switch sortBy {
        case .recent:
            return bucket.sorted { (a, b) in
                let ax = a.elapsedSeconds ?? 0
                let bx = b.elapsedSeconds ?? 0
                if ax == 0 && bx == 0 { return a.session < b.session }
                if ax == 0 { return false }
                if bx == 0 { return true }
                return ax < bx
            }
        case .oldest:
            return bucket.sorted { ($0.elapsedSeconds ?? 0) > ($1.elapsedSeconds ?? 0) }
        case .alpha:
            return bucket.sorted { $0.session < $1.session }
        }
    }

    var body: some View {
        NavigationStack {
            Group {
                if api.domains.isEmpty && !api.isConnected {
                    ContentUnavailableView {
                        Label("Not connected", systemImage: "wifi.slash")
                    } description: {
                        if let err = api.lastError {
                            Text(err)
                        } else {
                            Text("Check Server URL in Settings")
                        }
                    } actions: {
                        Button("Retry") { api.refresh() }
                            .buttonStyle(.borderedProminent)
                    }
                } else if allWorkers.isEmpty {
                    ContentUnavailableView("No workers",
                        systemImage: "cpu",
                        description: Text("Spin one up from Dispatch."))
                } else {
                    workersList
                }
            }
            .navigationTitle("Orchmux")
            .navigationBarTitleDisplayMode(.large)
            .searchable(text: $search, placement: .navigationBarDrawer(displayMode: .automatic),
                        prompt: "Filter workers")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    sortMenu
                }
                ToolbarItem(placement: .topBarTrailing) {
                    connectionDot
                }
            }
        }
    }

    // MARK: chrome

    private var sortMenu: some View {
        Menu {
            Picker("Sort", selection: $sortBy) {
                ForEach(SortKey.allCases) { k in
                    Label(k.rawValue, systemImage: k.symbol).tag(k)
                }
            }
        } label: {
            Image(systemName: "arrow.up.arrow.down.circle")
        }
    }

    private var connectionDot: some View {
        Image(systemName: api.isConnected ? "circle.fill" : "exclamationmark.triangle.fill")
            .foregroundStyle(api.isConnected ? .green : .orange)
            .symbolEffect(.pulse, options: .repeating, isActive: api.isConnected)
            .font(.caption)
            .accessibilityLabel(api.isConnected ? "Connected" : "Disconnected")
    }

    // MARK: list

    private var workersList: some View {
        List {
            Section {
                StatusSummaryCard(workers: allWorkers)
                    .listRowInsets(EdgeInsets(top: 4, leading: 14, bottom: 8, trailing: 14))
                    .listRowSeparator(.hidden)
                    .listRowBackground(Color.clear)
            }

            ForEach(groupedByStatus, id: \.0) { (status, workers) in
                Section {
                    if !collapsedSections.contains(status) {
                        ForEach(workers) { worker in
                            NavigationLink(destination: WorkerDetailView(worker: worker)) {
                                WorkerRowView(worker: worker)
                            }
                            .swipeActions(edge: .leading, allowsFullSwipe: false) {
                                Button {
                                    UIPasteboard.general.string = worker.session
                                } label: {
                                    Label("Copy", systemImage: "doc.on.doc")
                                }
                                .tint(.blue)
                            }
                        }
                    }
                } header: {
                    SectionHeader(status: status,
                                  count: workers.count,
                                  collapsed: collapsedSections.contains(status)) {
                        toggle(status)
                    }
                }
            }
        }
        .listStyle(.insetGrouped)
        .refreshable { api.refresh() }
        .animation(.default, value: collapsedSections)
        .animation(.default, value: sortBy)
    }

    private func toggle(_ status: Worker.WorkerStatus) {
        if collapsedSections.contains(status) {
            collapsedSections.remove(status)
        } else {
            collapsedSections.insert(status)
        }
    }
}

// MARK: - Section header (collapsible)

struct SectionHeader: View {
    let status: Worker.WorkerStatus
    let count: Int
    let collapsed: Bool
    let onTap: () -> Void

    var body: some View {
        Button(action: onTap) {
            HStack(spacing: 6) {
                Image(systemName: collapsed ? "chevron.right" : "chevron.down")
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(.secondary)
                Circle()
                    .fill(StatusBadge.color(for: status))
                    .frame(width: 7, height: 7)
                Text(status.label.uppercased())
                    .font(.caption.weight(.semibold))
                Text("\(count)")
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.secondary)
                Spacer()
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }
}

// MARK: - Status summary (top card)

/// Minimal status pill row, mirrors web `/clean/mobile.jsx`:
/// only shows non-zero buckets, no chrome, no bar.
struct StatusSummaryCard: View {
    let workers: [Worker]

    private var pills: [(Worker.WorkerStatus, Int)] {
        let order: [Worker.WorkerStatus] = [.busy, .waiting, .blocked, .idle, .missing]
        return order.compactMap { s in
            let n = workers.filter { $0.status == s }.count
            return n > 0 ? (s, n) : nil
        }
    }

    var body: some View {
        HStack(spacing: 14) {
            ForEach(pills, id: \.0) { (status, n) in
                HStack(spacing: 5) {
                    Circle()
                        .fill(StatusBadge.color(for: status))
                        .frame(width: 7, height: 7)
                    Text("\(n) \(status.label)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            Spacer()
        }
    }
}

// MARK: - Worker row

struct WorkerRowView: View {
    let worker: Worker

    private var primaryText: String {
        if let task = worker.currentTask, !task.isEmpty, task != worker.session {
            return task
        }
        return worker.session
    }

    private var primaryIsTask: Bool {
        if let task = worker.currentTask, !task.isEmpty, task != worker.session { return true }
        return false
    }

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            StatusIndicator(status: worker.status)
                .padding(.top, 3)

            VStack(alignment: .leading, spacing: 2) {
                Text(primaryText)
                    .font(primaryIsTask ? .system(size: 14) : .system(size: 14, design: .monospaced))
                    .fontWeight(primaryIsTask ? .medium : .semibold)
                    .lineLimit(2)

                HStack(spacing: 6) {
                    if primaryIsTask {
                        Text(worker.session)
                            .font(.system(size: 11, design: .monospaced))
                            .foregroundStyle(.secondary)
                    }
                    if primaryIsTask || worker.domain != worker.session {
                        if primaryIsTask {
                            Text("·").foregroundStyle(.tertiary)
                        }
                        Text(worker.domain)
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                    if let elapsed = worker.elapsedFormatted, isActive {
                        Text("·").foregroundStyle(.tertiary)
                        Text(elapsed)
                            .font(.caption2.monospacedDigit())
                            .foregroundStyle(StatusBadge.color(for: worker.status))
                    }
                }
            }
        }
        .padding(.vertical, 2)
    }

    private var isActive: Bool {
        worker.status == .busy || worker.status == .waiting || worker.status == .blocked
    }
}

// MARK: - Status indicator (animated dot)

struct StatusIndicator: View {
    let status: Worker.WorkerStatus

    var body: some View {
        ZStack {
            Circle()
                .strokeBorder(ringColor, lineWidth: 1.5)
                .frame(width: 18, height: 18)
            content
                .symbolEffect(.pulse, options: .repeating, isActive: isAnimating)
        }
    }

    @ViewBuilder
    private var content: some View {
        switch status {
        case .busy, .waiting:
            Image(systemName: "circle.fill")
                .font(.system(size: 8))
                .foregroundStyle(StatusBadge.color(for: status))
        case .blocked:
            Image(systemName: "exclamationmark")
                .font(.system(size: 10, weight: .bold))
                .foregroundStyle(StatusBadge.color(for: .blocked))
        case .idle:
            Capsule()
                .fill(Color.gray.opacity(0.45))
                .frame(width: 9, height: 2)
        case .missing, .offline:
            Capsule()
                .fill(Color.gray.opacity(0.25))
                .frame(width: 9, height: 2)
        case .unknown:
            EmptyView()
        }
    }

    private var ringColor: Color {
        switch status {
        case .busy, .waiting: return StatusBadge.color(for: status)
        case .blocked:        return StatusBadge.color(for: .blocked).opacity(0.7)
        default:              return Color.gray.opacity(0.25)
        }
    }

    private var isAnimating: Bool {
        status == .busy || status == .waiting
    }
}

// MARK: - Status badge (used in detail headers)

struct StatusBadge: View {
    let status: Worker.WorkerStatus

    var body: some View {
        Text(status.label)
            .font(.caption2)
            .fontWeight(.semibold)
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(Self.color(for: status).opacity(0.15))
            .foregroundStyle(Self.color(for: status))
            .clipShape(Capsule())
    }

    /// Mirrors web `STATUS_DOT` palette in /clean/shared.jsx.
    static func color(for status: Worker.WorkerStatus) -> Color {
        switch status {
        case .busy:    return Color(red: 0.96, green: 0.65, blue: 0.14)   // #f5a623
        case .waiting: return Color(red: 0.29, green: 0.56, blue: 0.89)   // #4a90e2
        case .blocked: return Color(red: 0.90, green: 0.35, blue: 0.42)   // #e55a6a
        case .idle:    return Color(red: 0.78, green: 0.77, blue: 0.73)   // #c8c4ba
        case .missing: return Color(red: 0.78, green: 0.77, blue: 0.73)
        case .offline: return Color(red: 0.65, green: 0.65, blue: 0.65)
        case .unknown: return .gray
        }
    }
}
