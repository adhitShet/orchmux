import SwiftUI

struct CompletedView: View {
    @EnvironmentObject var api: APIClient
    @State private var isRefreshing: Bool = false

    var body: some View {
        NavigationStack {
            Group {
                if api.completed.isEmpty {
                    VStack(spacing: 12) {
                        Image(systemName: "checkmark.seal")
                            .font(.system(size: 48))
                            .foregroundStyle(.secondary)
                        Text("No completed tasks")
                            .foregroundStyle(.secondary)
                    }
                } else {
                    List {
                        ForEach(api.completed) { c in
                            VStack(alignment: .leading, spacing: 6) {
                                HStack {
                                    Image(systemName: c.success ? "checkmark.circle.fill" : "xmark.octagon.fill")
                                        .foregroundStyle(c.success ? .green : .red)
                                    Text(c.session.isEmpty ? c.id : c.session)
                                        .font(.headline)
                                    Spacer()
                                    Text(c.id)
                                        .font(.caption2.monospaced())
                                        .foregroundStyle(.tertiary)
                                }
                                if !c.result.isEmpty {
                                    Text(c.result)
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                        .lineLimit(4)
                                        .textSelection(.enabled)
                                }
                            }
                            .padding(.vertical, 2)
                        }
                    }
                    .listStyle(.insetGrouped)
                }
            }
            .navigationTitle("Completed")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button(action: { Task { await refresh() } }) {
                        Image(systemName: "arrow.clockwise")
                    }
                }
            }
            .refreshable { await refresh() }
            .task { await refresh() }
        }
    }

    @MainActor
    private func refresh() async {
        isRefreshing = true
        await api.fetchCompleted()
        isRefreshing = false
    }
}
