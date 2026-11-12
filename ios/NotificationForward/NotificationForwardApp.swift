import SwiftUI
import UserNotifications

@main
struct NotificationForwardApp: App {
    @State private var manager = AccessoryManager()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environment(manager)
                .task {
                    manager.activate()
                    // Needed for the "notification not forwarded" warning the
                    // transport extension shows when Home Assistant is unreachable.
                    _ = try? await UNUserNotificationCenter.current()
                        .requestAuthorization(options: [.alert, .sound, .badge])
                }
        }
    }
}
