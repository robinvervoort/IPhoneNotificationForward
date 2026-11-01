import SwiftUI

struct ContentView: View {
    @Environment(AccessoryManager.self) private var manager
    @State private var webhookText = ""
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
                    TextField("https://your-ha.example.com/api/webhook/…", text: $webhookText)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .onSubmit(saveWebhook)
                    Button("Save", action: saveWebhook)
                        .disabled(parsedWebhookURL == nil)
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
            .navigationTitle("Notification Forward")
            .refreshable { await manager.refreshStatus() }
            .onAppear {
                savedURL = SharedSettings.webhookURL
                webhookText = savedURL?.absoluteString ?? ""
            }
        }
    }

    private func saveWebhook() {
        guard let url = parsedWebhookURL else { return }
        SharedSettings.webhookURL = url
        savedURL = url
    }

    /// Accepts only a full https URL to a Home Assistant webhook.
    private var parsedWebhookURL: URL? {
        let text = webhookText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let url = URL(string: text), url.scheme == "https", url.host != nil,
              url.path.contains("/api/webhook/") else { return nil }
        return url
    }
}
