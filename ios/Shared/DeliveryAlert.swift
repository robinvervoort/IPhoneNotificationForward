import Foundation
import UserNotifications

/// Warns on the iPhone itself when a notification could not be delivered to
/// Home Assistant (bridge out of range and Home Assistant unreachable, or
/// Home Assistant down). This works even when Home Assistant cannot send
/// anything itself.
///
/// A failure is not reported straight away: iOS retries and may pick another
/// transport, so the warning is scheduled `grace` seconds ahead and cancelled
/// when a later delivery succeeds. After a warning has been shown, the next
/// one waits `quietPeriod`, so a burst of failed notifications gives one alert.
enum DeliveryAlert {
    static let identifier = "nf-delivery-failed"
    static let grace: TimeInterval = 60
    static let quietPeriod: TimeInterval = 15 * 60

    /// Schedules the warning unless one is already pending or was shown recently.
    static func deliveryFailed(reason: String, delay: TimeInterval = grace) async {
        let center = UNUserNotificationCenter.current()
        let settings = await center.notificationSettings()
        guard settings.authorizationStatus == .authorized else {
            DiagnosticLog.log("Delivery alert: notifications not allowed (\(settings.authorizationStatus.rawValue))")
            return
        }
        if await center.pendingNotificationRequests().contains(where: { $0.identifier == identifier }) {
            return
        }
        if let shown = await center.deliveredNotifications().first(where: { $0.request.identifier == identifier }),
           Date().timeIntervalSince(shown.date) < quietPeriod {
            return
        }

        let content = UNMutableNotificationContent()
        content.title = "Notification not forwarded"
        content.body = "A notification could not be delivered to Home Assistant (\(reason)). "
            + "Check your iPhone directly: alerts may not reach Home Assistant."
        content.sound = .default
        content.interruptionLevel = .timeSensitive
        content.threadIdentifier = "nf-delivery"

        let trigger = UNTimeIntervalNotificationTrigger(timeInterval: max(delay, 1), repeats: false)
        do {
            try await center.add(UNNotificationRequest(identifier: identifier, content: content, trigger: trigger))
            SharedSettings.deliveryAlertPending = true
            DiagnosticLog.log("Delivery alert scheduled in \(Int(delay)) s (\(reason))")
        } catch {
            DiagnosticLog.log("Delivery alert could not be scheduled: \(error.localizedDescription)")
        }
    }

    /// A delivery worked: cancel a warning that has not been shown yet.
    static func delivered() {
        guard SharedSettings.deliveryAlertPending else { return }
        SharedSettings.deliveryAlertPending = false
        UNUserNotificationCenter.current().removePendingNotificationRequests(withIdentifiers: [identifier])
        DiagnosticLog.log("Delivery alert cancelled: delivery works again")
    }
}
