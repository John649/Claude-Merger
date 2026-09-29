import Foundation
import Combine

// Updates are deliberately unavailable in this local build at the user's request.
@MainActor
final class Updater: NSObject, ObservableObject {
    @Published private(set) var availableVersion: String? = nil
    @Published private(set) var canCheck = false
    static let isRelaunchingForUpdate = false
    nonisolated override init() { super.init() }
    func start() {}
    func checkForUpdates() {}
}
