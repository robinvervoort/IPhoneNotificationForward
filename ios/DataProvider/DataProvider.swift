import AccessoryNotifications
import AccessoryTransportExtension
import Foundation

/// Receives forwarded iOS notifications (in plaintext, inside this sandboxed
/// extension) and curates them into the JSON the Home Assistant bridge expects.
/// The system encrypts the result for the bridge before it leaves the extension.
@main
struct DataProvider: AccessoryDataProvider {
    var extensionPoint: AppExtensionPoint {
        Identifier("com.apple.accessory-data-provider")
        Implementing {
            NotificationsForwarding {
                NotificationHandler()
            }
        }
    }
}

/// JSON schema v1, decoded by `addon/deskbot_notify/app/ha.py`.
struct NotificationPayload: Encodable {
    var v = 1
    var action: String
    var id: String?
    var app: String?
    var title: String?
    var subtitle: String?
    var body: String?
    var summary: String?
    var thread: String?
    var date: Date?
    var attributes: String?
    var shouldAlert: Bool?
    var focusSuppressed: Bool?

    enum CodingKeys: String, CodingKey {
        case v, action, id, app, title, subtitle, body, summary, thread, date, attributes
        case shouldAlert = "should_alert"
        case focusSuppressed = "focus_suppressed"
    }
}

final class NotificationHandler: AccessoryNotificationsHandler {
    var session: NotificationsForwarding.Session?

    private let encoder: JSONEncoder = {
        let e = JSONEncoder()
        e.dateEncodingStrategy = .iso8601
        return e
    }()

    func didActivate(for session: NotificationsForwarding.Session) {
        self.session = session
    }

    func addNotification(_ notification: AccessoryNotification,
                         alertingContext: AlertingContext) async throws -> Bool {
        var payload = makePayload(notification, action: "add")
        payload.shouldAlert = alertingContext.shouldAlert
        payload.focusSuppressed = alertingContext.isSuppressedByFocus
        try await send(payload)
        // Home Assistant doesn't alert a person itself, so report `false` and
        // let the iPhone (or a watch) keep alerting as usual.
        return false
    }

    func updateNotification(_ notification: AccessoryNotification) {
        let payload = makePayload(notification, action: "update")
        Task { try? await send(payload) }
    }

    func removeNotification(identifier: AccessoryNotification.Identifier) {
        let payload = NotificationPayload(action: "remove", id: String(describing: identifier))
        Task { try? await send(payload) }
    }

    func removeAllNotifications() {
        Task { try? await send(NotificationPayload(action: "remove_all")) }
    }

    func messageHandler(_ message: AccessoryMessage) {
        // The bridge never sends responses (no dismiss/reply from Home Assistant yet).
    }

    func didInvalidate() {
        session = nil
    }

    // MARK: - Helpers

    private func makePayload(_ n: AccessoryNotification, action: String) -> NotificationPayload {
        NotificationPayload(
            action: action,
            id: String(describing: n.identifier),
            app: n.sourceName,
            title: n.title,
            subtitle: n.subtitle,
            body: n.body?.string,
            summary: n.summary?.string,
            thread: n.threadIdentifier,
            date: n.deliveryDate,
            attributes: String(describing: n.attributes)
        )
    }

    private func send(_ payload: NotificationPayload) async throws {
        let data = try encoder.encode(payload)
        // Same JSON for every transport; no icons/attachments, so it stays small.
        let message = AccessoryMessage {
            AccessoryMessage.Payload(transport: .bluetooth, data: data)
            AccessoryMessage.Payload(transport: .internet, data: data)
        }
        try await session?.send(message: message)
    }
}
