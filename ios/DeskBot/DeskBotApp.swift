import SwiftUI

@main
struct DeskBotApp: App {
    @State private var manager = AccessoryManager()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environment(manager)
                .task { manager.activate() }
        }
    }
}
