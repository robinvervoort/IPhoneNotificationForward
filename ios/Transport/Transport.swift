import AccessoryTransportExtension
import Foundation

/// Relays already-encrypted notification data to the bridge:
/// over Bluetooth when the bridge is in range, otherwise over HTTPS to the
/// Home Assistant webhook. This extension cannot read the content.
@main
struct Transport: AccessoryTransportAppExtension {
    func accept(sessionRequest: AccessoryTransportSession.Request) -> AccessoryTransportSession.Request.Decision {
        sessionRequest.accept {
            TransportEventHandler(session: sessionRequest.session)
        }
    }
}

final class TransportEventHandler: AccessoryTransportSession.EventHandler {
    private let session: AccessoryTransportSession
    private var link: BLELink?

    init(session: AccessoryTransportSession) {
        self.session = session
    }

    func messageReceived(_ message: TransportMessage,
                         completion: @escaping @Sendable (AccessoryMessage.Result) -> Void) {
        let transport = session.transport
        Task {
            do {
                switch transport {
                case .bluetooth:
                    try await sendOverBluetooth(message)
                case .internet, .localNetwork:
                    try await sendOverInternet(message)
                default: // nil (no transport) or a future transport type
                    completion(.failure(.transportUnavailable))
                    return
                }
                completion(.success)
            } catch BridgeError.notPaired, BridgeError.noWebhook, BridgeError.peripheralNotFound {
                completion(.failure(.transportUnavailable))
            } catch {
                completion(.failure(.transportFailed)) // the system retries / picks another transport
            }
        }
    }

    func sessionInvalidated(error: AccessoryTransportSession.Error?) {
        link?.disconnect()
        link = nil
    }

    // MARK: - Bluetooth

    private func bluetoothLink() throws -> BLELink {
        if let link { return link }
        guard let id = SharedSettings.accessoryBluetoothID else { throw BridgeError.notPaired }
        // One restorable central per extension instance, as Apple requires for
        // background relaunch when the bridge notifies.
        let link = BLELink(peripheralID: id, restoreIdentifier: session.transportStateRestoreIdentifier)
        self.link = link
        return link
    }

    private func sendOverBluetooth(_ message: TransportMessage) async throws {
        let frame = BridgeProtocol.data(kind: .ciphertext, sessionID: message.sessionID, payload: message.data)
        try await bluetoothLink().send(frame)
    }

    // MARK: - Internet (Home Assistant webhook -> add-on)

    private struct WebhookBody: Encodable {
        let data: String
        let sessionID: String
        let pushToken: String?

        enum CodingKeys: String, CodingKey {
            case data
            case sessionID = "session_id"
            case pushToken = "push_token"
        }
    }

    private func sendOverInternet(_ message: TransportMessage) async throws {
        guard let url = SharedSettings.webhookURL else { throw BridgeError.noWebhook }
        let body = WebhookBody(
            data: message.data.base64EncodedString(),
            sessionID: message.sessionID.uuidString,
            pushToken: session.pushToken.map { $0.map { String(format: "%02x", $0) }.joined() }
        )
        var request = URLRequest(url: url, timeoutInterval: 20)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(body)
        let (_, response) = try await URLSession.shared.data(for: request)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(status) else { throw BridgeError.http(status) }
    }
}
