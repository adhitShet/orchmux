import SwiftUI

struct ContentView: View {
    @EnvironmentObject var api: APIClient
    @State private var selectedTab = 0

    var body: some View {
        TabView(selection: $selectedTab) {
            DashboardView()
                .tabItem { Label("Workers", systemImage: "cpu") }
                .tag(0)

            DispatchView()
                .tabItem { Label("Dispatch", systemImage: "paperplane.fill") }
                .tag(1)

            QuestionsView()
                .tabItem {
                    Label("Questions", systemImage: "questionmark.bubble.fill")
                }
                .tag(2)
                .badge(api.questions.count > 0 ? api.questions.count : 0)

            TodosView()
                .tabItem { Label("Notes", systemImage: "note.text") }
                .tag(3)

            SettingsView()
                .tabItem { Label("Settings", systemImage: "gearshape.fill") }
                .tag(4)
        }
        .onAppear { api.startPolling() }
    }
}
