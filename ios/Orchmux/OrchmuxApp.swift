import SwiftUI

@main
struct OrchmuxApp: App {
    @StateObject private var config = Config.shared
    @StateObject private var api = APIClient.shared

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(config)
                .environmentObject(api)
                .task { await config.syncVaultInfo(from: api) }
        }
    }
}
