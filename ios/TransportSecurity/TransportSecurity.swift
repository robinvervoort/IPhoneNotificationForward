import AccessoryTransportExtension
import ExtensionFoundation
import Foundation

/// Runs the HPKE key exchange between iOS and the bridge.
///
/// 1. Fetch the bridge's long-term X-Wing public key over Bluetooth.
/// 2. Hand it to the system, which answers with an encapsulated key.
/// 3. Relay the encapsulated key to the bridge, which derives the same secrets.
///
/// X-Wing (post-quantum) is required for the internet transport.
@main
struct TransportSecurity: AccessoryTransportSecurity {
    @AppExtensionPoint.Bind
    static var boundExtensionPoint: AppExtensionPoint {
        Identifier("com.apple.accessory-transport-security")
    }

    func accept(sessionRequest: AccessorySecuritySession.Request) -> AccessorySecuritySession.Request.Decision {
        sessionRequest.accept {
            SecurityEventHandler(session: sessionRequest.session)
        }
    }
}

final class SecurityEventHandler: AccessorySecuritySession.EventHandler {
    private let session: AccessorySecuritySession
    private var link: BLELink?
    private var identifier: String?

    init(session: AccessorySecuritySession) {
        self.session = session
        Task { await self.startKeyExchange() }
    }

    private func startKeyExchange() async {
        do {
            guard let peripheralID = SharedSettings.accessoryBluetoothID else { throw BridgeError.notPaired }
            let link = BLELink(peripheralID: peripheralID)
            self.link = link
            identifier = peripheralID.uuidString

            let response = try await link.request(BridgeProtocol.getPublicKey(.xWing))
            let (_, publicKey) = try BridgeProtocol.parsePublicKey(response)

            let message = SecurityMessage(
                keyType: .publicKey,
                cipherSuite: .xWing,
                version: .version1,
                key: publicKey,
                supportedTransports: [.bluetooth, .internet],
                identifier: peripheralID.uuidString
            )
            try session.sendSecurityMessage(message)
        } catch {
            link?.disconnect()
            link = nil
            session.cancel(error: .unknown)
        }
    }

    func messageReceived(_ message: SecurityMessage,
                         completion: @escaping @Sendable (AccessoryMessage.Result) -> Void) {
        guard message.keyType == .encapsulatedKey else {
            completion(.success)
            return
        }
        guard let link, let ident = message.identifier ?? identifier else {
            completion(.failure(.transportFailed))
            return
        }
        let suite: BridgeProtocol.Suite = message.cipherSuite == .p256 ? .p256 : .xWing
        let protocolInfo = "\(message.cipherSuite)-\(message.version)-\(ident)"
        let request = BridgeProtocol.keyExchange(suite: suite, identifier: ident, protocolInfo: protocolInfo,
                                                 encapsulatedKey: message.key)
        Task {
            do {
                try BridgeProtocol.checkResult(try await link.request(request))
                completion(.success)
            } catch {
                completion(.failure(.transportFailed))
            }
        }
    }

    func sessionInvalidated(error: AccessorySecuritySession.Error?) {
        link?.disconnect()
        link = nil
    }
}
