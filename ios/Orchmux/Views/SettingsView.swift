import SwiftUI

struct SettingsView: View {
    @EnvironmentObject var config: Config
    @EnvironmentObject var api: APIClient
    @State private var editingURL = ""
    @State private var editingToken = ""
    @State private var editingVault = ""
    @State private var editingNotesPath = ""
    @State private var isTesting = false
    @State private var testResult: String?

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("Server URL", text: $editingURL)
                        .keyboardType(.URL)
                        .autocorrectionDisabled()
                        .textInputAutocapitalization(.never)
                        .onSubmit { save() }
                } header: {
                    Text("Server")
                } footer: {
                    Text("Tailscale IP, e.g. http://100.64.0.1:9889")
                }

                Section {
                    TextField("Vault Token (optional)", text: $editingToken)
                        .autocorrectionDisabled()
                        .textInputAutocapitalization(.never)
                } header: {
                    Text("Auth")
                } footer: {
                    Text("Required for /vault and /dispatch-history endpoints")
                }

                Section {
                    Button("Save & Test") {
                        save()
                        test()
                    }
                    .disabled(isTesting)

                    if let result = testResult {
                        Label(result, systemImage: result.hasPrefix("✓") ? "checkmark.circle.fill" : "xmark.circle.fill")
                            .foregroundStyle(result.hasPrefix("✓") ? .green : .red)
                            .font(.caption)
                    }
                }

                Section("Connection") {
                    LabeledContent("Status") {
                        HStack {
                            Circle()
                                .fill(api.isConnected ? Color.green : Color.red)
                                .frame(width: 8, height: 8)
                            Text(api.isConnected ? "Connected" : "Disconnected")
                        }
                    }
                    if let err = api.lastError {
                        Text(err)
                            .font(.caption)
                            .foregroundStyle(.red)
                    }
                    LabeledContent("Workers", value: "\(api.domains.flatMap(\.workers).count)")
                    LabeledContent("Pending Qs", value: "\(api.questions.count)")
                }

                Section {
                    TextField("Vault name", text: $editingVault)
                        .autocorrectionDisabled()
                        .textInputAutocapitalization(.never)
                        .onSubmit { save() }
                    TextField("Notes path inside vault", text: $editingNotesPath)
                        .autocorrectionDisabled()
                        .textInputAutocapitalization(.never)
                        .onSubmit { save() }
                    Button {
                        if let url = URL(string: "obsidian://open?vault=\(editingVault)") {
                            UIApplication.shared.open(url)
                        }
                    } label: {
                        Label("Test — open vault in Obsidian", systemImage: "arrow.up.right.square")
                    }
                } header: {
                    Text("Obsidian")
                } footer: {
                    Text("Vault name must match exactly what the Obsidian app shows in its vault switcher (case-sensitive). Default is “obsidian-vault”.")
                }

                Section("About") {
                    LabeledContent("App", value: "Orchmux iOS")
                    LabeledContent("Polling", value: "Every 3 seconds")
                    LabeledContent("Repo", value: "adhitShet/orchmux")
                }
            }
            .navigationTitle("Settings")
            .onAppear {
                editingURL        = config.serverURL
                editingToken      = config.vaultToken
                editingVault      = config.obsidianVault
                editingNotesPath  = config.obsidianNotesPath
            }
        }
    }

    private func save() {
        config.serverURL         = editingURL.trimmingCharacters(in: .whitespaces)
        config.vaultToken        = editingToken.trimmingCharacters(in: .whitespaces)
        config.obsidianVault     = editingVault.trimmingCharacters(in: .whitespaces)
        config.obsidianNotesPath = editingNotesPath.trimmingCharacters(in: .whitespaces)
        api.refresh()
    }

    private func test() {
        isTesting = true
        testResult = nil
        Task {
            guard let url = URL(string: "\(config.serverURL)/status") else {
                testResult = "✗ Invalid URL"
                isTesting = false
                return
            }
            do {
                let (_, response) = try await APIClient.urlSession.data(from: url)
                if let http = response as? HTTPURLResponse, http.statusCode == 200 {
                    testResult = "✓ Connected (\(http.statusCode))"
                } else {
                    testResult = "✗ Server returned error"
                }
            } catch {
                testResult = "✗ \(error.localizedDescription)"
            }
            isTesting = false
        }
    }
}
