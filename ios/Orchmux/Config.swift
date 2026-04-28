import Foundation
import Combine

final class Config: ObservableObject {
    static let shared = Config()

    @Published var serverURL: String {
        didSet { UserDefaults.standard.set(serverURL, forKey: "serverURL") }
    }

    @Published var vaultToken: String {
        didSet { UserDefaults.standard.set(vaultToken, forKey: "vaultToken") }
    }

    /// Obsidian vault name — must match what the Obsidian app shows in its vault switcher
    /// (NOT the folder path). Used for `obsidian://open?vault=…` deep links.
    @Published var obsidianVault: String {
        didSet { UserDefaults.standard.set(obsidianVault, forKey: "obsidianVault") }
    }

    /// Path inside the vault where session notes live.
    @Published var obsidianNotesPath: String {
        didSet { UserDefaults.standard.set(obsidianNotesPath, forKey: "obsidianNotesPath") }
    }

    private init() {
        serverURL  = UserDefaults.standard.string(forKey: "serverURL")  ?? "http://localhost:9889"
        vaultToken = UserDefaults.standard.string(forKey: "vaultToken") ?? ""
        // Default to "" so we don't hardcode; /vault-info populates these on launch.
        obsidianVault     = UserDefaults.standard.string(forKey: "obsidianVault")     ?? ""
        obsidianNotesPath = UserDefaults.standard.string(forKey: "obsidianNotesPath") ?? ""
    }

    /// Server-of-truth pull: ask `/vault-info` and update cached values.
    /// Called once on app launch. Silent-fails if the endpoint 404s or the server
    /// is unreachable — keeps whatever was in UserDefaults.
    @MainActor
    func syncVaultInfo(from client: APIClient) async {
        do {
            let info = try await client.fetchVaultInfo()
            if !info.vault_name.isEmpty { obsidianVault = info.vault_name }
            if let np = info.notes_path, !np.isEmpty { obsidianNotesPath = np }
        } catch {
            // keep cached values
        }
    }

    var baseURL: URL { URL(string: serverURL) ?? URL(string: "http://localhost:9889")! }
}
