import AccessoryNotifications
import AccessoryTransportExtension
import ExtensionFoundation
import Foundation
import Synchronization

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

/// JSON schema v1, decoded by `addon/notification_forward/app/ha.py`.
struct NotificationPayload: Encodable {
    var v = 1
    var action: String
    var id: String?
    var bundleID: String?
    var app: String?
    var title: String?
    var subtitle: String?
    var body: String?
    var summary: String?
    var thread: String?
    var date: Date?
    var attributes: [String]?
    var shouldAlert: Bool?
    var focusSuppressed: Bool?

    enum CodingKeys: String, CodingKey {
        case v, action, id, app, title, subtitle, body, summary, thread, date, attributes
        case bundleID = "bundle_id"
        case shouldAlert = "should_alert"
        case focusSuppressed = "focus_suppressed"
    }
}

final class NotificationHandler: NotificationsForwarding.AccessoryNotificationsHandler {
    /// The handler must be Sendable; the system may call it from any thread.
    private let session = Mutex<NotificationsForwarding.Session?>(nil)

    private let encoder: JSONEncoder = {
        let e = JSONEncoder()
        e.dateEncodingStrategy = .iso8601
        return e
    }()

    func didActivate(for session: NotificationsForwarding.Session) {
        self.session.withLock { $0 = session }
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
        let payload = NotificationPayload(action: "remove", id: identifier.notificationIdentifier,
                                          bundleID: identifier.sourceIdentifier)
        Task { try? await send(payload) }
    }

    func removeAllNotifications() {
        Task { try? await send(NotificationPayload(action: "remove_all")) }
    }

    func messageHandler(_ message: TransportMessage) {
        // The bridge never sends responses (no dismiss/reply from Home Assistant yet).
    }

    func didInvalidate() {
        session.withLock { $0 = nil }
    }

    // MARK: - Helpers

    private func makePayload(_ n: AccessoryNotification, action: String) -> NotificationPayload {
        NotificationPayload(
            action: action,
            id: n.identifier.notificationIdentifier,
            bundleID: n.identifier.sourceIdentifier,
            app: n.sourceName,
            title: n.title,
            subtitle: n.subtitle,
            body: n.body?.string,
            summary: n.summary?.string,
            thread: n.threadIdentifier,
            date: n.deliveryDate,
            attributes: Self.attributeNames(n.attributes)
        )
    }

    private func send(_ payload: NotificationPayload) async throws {
        let data = try encoder.encode(payload)
        // Same JSON for every transport; no icons/attachments, so it stays small.
        let message = AccessoryMessage {
            AccessoryMessage.Payload(transport: .bluetooth, data: data)
            AccessoryMessage.Payload(transport: .internet, data: data)
        }
        guard let session = self.session.withLock({ $0 }) else { return }
        try await session.send(message: message)
    }

    private static func attributeNames(_ a: AccessoryNotification.Attributes) -> [String] {
        var names: [String] = []
        if a.contains(.critical) { names.append("critical") }
        if a.contains(.timeSensitive) { names.append("time_sensitive") }
        if a.contains(.priority) { names.append("priority") }
        return names
    }
}
