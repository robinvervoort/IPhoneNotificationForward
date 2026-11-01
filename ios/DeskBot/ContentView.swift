import SwiftUI

struct ContentView: View {
    @Environment(AccessoryManager.self) private var manager
    @State private var webhookID = ""
    @State private var savedURL: URL?

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    LabeledContent("Status", value: manager.status)
                    if manager.accessory == nil {
                        Button("Pair bridge") { manager.pair() }
                    } else {
                        Button("Allow notification forwarding") {
                            Task { await manager.requestForwarding() }
                        }
                        Button("Remove bridge", role: .destructive) { manager.unpair() }
                    }
                } header: {
                    Text("Home Assistant bridge")
                } footer: {
                    Text("Stay near the Bluetooth dongle on your Home Assistant server while pairing.")
                }

                Section {
                    TextField("Webhook ID", text: $webhookID)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .onSubmit(saveWebhook)
                    Button("Save", action: saveWebhook)
                        .disabled(webhookID.trimmingCharacters(in: .whitespaces).isEmpty)
                    if let url = savedURL {
                        Text(url.absoluteString).font(.footnote).foregroundStyle(.secondary)
                    }
                } header: {
                    Text("Away from home")
                } footer: {
                    Text("Used when the bridge is out of Bluetooth range. Notifications stay end-to-end "
                         + "encrypted; Home Assistant only relays ciphertext to the add-on.")
                }

                if let error = manager.lastError {
                    Section("Last error") {
                        Text(error).foregroundStyle(.red)
                    }
                }
            }
            .navigationTitle("DeskBot")
            .refreshable { await manager.refreshStatus() }
            .onAppear {
                savedURL = SharedSettings.webhookURL
                webhookID = savedURL?.lastPathComponent ?? ""
            }
        }
    }

    private func saveWebhook() {
        let id = webhookID.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !id.isEmpty else { return }
        SharedSettings.webhookURL = URL(string: SharedSettings.defaultWebhookBase + id)
        savedURL = SharedSettings.webhookURL
    }
}
