import AppKit
import Foundation

/// Merges chats without waiting for a shortcut to be opened.
///
/// A launcher squares its profile up on its way to opening Claude, which left
/// merging to whichever shortcut happened to be clicked next: a group joined,
/// or chats left in another profile, sat unmerged until then. This runs the
/// same passes — graft each profile onto its source, bring across chats an
/// account left elsewhere, mirror every pair, file missing records — from the
/// app, either on a press or on its own whenever every Claude is closed.
///
/// Nothing moves while any Claude is up, for the reason `adoptChats` gives: an
/// instance builds its sidebar as it starts and rewrites records as it runs.
/// The press can quit them first and reopen them afterwards; the timer only
/// ever waits.
final class ChatMerger: ObservableObject {
    @Published private(set) var merging = false
    /// What the last pass did, for a line under the button.
    @Published private(set) var note: String?

    /// Shortcuts whose source changed since their folders were last grafted.
    /// Only these get `apply` on the timer; the press grafts everything.
    private var pending: Set<UUID> = []
    private var timer: Timer?
    private weak var store: ShortcutStore?

    static let autoKey = "autoMergeChats"

    static var automatic: Bool {
        get { UserDefaults.standard.object(forKey: autoKey) as? Bool ?? true }
        set { UserDefaults.standard.set(newValue, forKey: autoKey) }
    }

    func start(watching store: ShortcutStore) {
        self.store = store
        timer?.invalidate()
        // Two minutes is often enough to catch the gap between quitting one
        // Claude and opening the next, and rare enough that the pgrep per
        // profile it costs is nothing.
        timer = Timer.scheduledTimer(withTimeInterval: 120, repeats: true) { [weak self] _ in
            guard let self, let store = self.store, ChatMerger.automatic else { return }
            self.merge(store, quitting: false, quiet: true)
        }
    }

    func markChanged(_ ids: [UUID]) {
        pending.formUnion(ids)
        guard let store else { return }
        let configs = ids.compactMap { id in
            store.shortcut(id).map { (shortcut: $0, sourceDir: store.sourceDir(for: $0)) }
        }
        // The bundle's graft.json is what the Dock runs, so it has to agree
        // with the list the moment the list changes, not at the next launch
        // of this app.
        DispatchQueue.global(qos: .utility).async {
            for config in configs {
                Installer.refreshConfig(for: config.shortcut, sourceDir: config.sourceDir)
            }
        }
        if ChatMerger.automatic { merge(store, quitting: false, quiet: true) }
    }

    /// One pass. `quitting` quits every running Claude first and reopens the
    /// same ones afterwards; without it a running Claude means nothing moves.
    /// `quiet` is the timer's version: no note unless something arrived.
    func merge(_ store: ShortcutStore, quitting: Bool, quiet: Bool = false,
               done: ((_ blockedBy: [URL]) -> Void)? = nil) {
        guard !merging else { return }
        merging = true
        if !quiet { note = nil }

        let everything = !quiet || quitting
        let installed = store.shortcuts.filter { $0.installedName != nil }
        // Roots first, so a profile grafted from another is filled from a
        // source that has already been squared up this pass.
        let ordered = installed.sorted { depth(of: $0, in: store) < depth(of: $1, in: store) }
        let grafts = ordered
            .filter { everything || pending.contains($0.id) }
            .map { (id: $0.id, config: GraftConfig(profileDir: $0.profileDir.path,
                                                    sourceDir: store.sourceDir(for: $0)?.path)) }
        let ownProfiles = installed.filter { $0.source == .own }.map(\.profileDir)
        let filing = [Graft.mainProfile] + installed.map(\.profileDir)
        let bundles = Dictionary(uniqueKeysWithValues: installed.compactMap { shortcut in
            Installer.installedBundle(for: shortcut).map { (shortcut.profileDir.path, $0) }
        })

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            var running = Graft.runningClaudes()
            var reopen: [URL] = []

            if !running.isEmpty, quitting {
                reopen = running
                Self.quit(running)
                running = Graft.runningClaudes()
            }
            guard running.isEmpty else {
                DispatchQueue.main.async {
                    guard let self else { return }
                    self.merging = false
                    if !quiet {
                        self.note = L10n.format("Nothing merged — quit %@ first",
                                                running.map(store.name(ofProfile:))
                                                    .joined(separator: ", "))
                    }
                    done?(running)
                }
                return
            }

            for graft in grafts { Graft.apply(graft.config) }

            var adopted = 0
            let everyStore = Graft.sessionStoreProfiles()
            for profile in ownProfiles {
                guard let found = Graft.chatsElsewhere(for: profile, among: everyStore) else { continue }
                adopted += Graft.adoptChats(from: found.profile, into: profile,
                                            account: found.account).copied
            }
            let moved = Graft.mirrorKnownPairs()
            let filed = Graft.squareUp(filingInto: filing)

            // Through the launcher for a shortcut, since that is what grafts
            // it on the way in; Claude's own profile has none.
            for profile in reopen {
                if let bundle = bundles[profile.path] {
                    DispatchQueue.main.async {
                        NSWorkspace.shared.openApplication(at: bundle,
                                                           configuration: NSWorkspace.OpenConfiguration())
                    }
                } else {
                    Graft.open(profile: profile)
                }
            }

            DispatchQueue.main.async {
                guard let self else { return }
                self.merging = false
                self.pending.subtract(grafts.map(\.id))
                let changes = adopted + moved + filed.count
                if !quiet || changes > 0 {
                    self.note = changes == 0
                        ? L10n.text("Chats are already merged")
                        : L10n.format("Merged: %ld chats brought across, %ld changes synced",
                                      adopted + filed.count, moved)
                }
                done?([])
            }
        }
    }

    private func depth(of shortcut: Shortcut, in store: ShortcutStore) -> Int {
        var depth = 0
        var current = shortcut
        while case .shortcut(let id) = current.source, let next = store.shortcut(id), depth < 32 {
            depth += 1
            current = next
        }
        return depth
    }

    /// Asks each Claude to quit the way the Dock would, and waits for them.
    private static func quit(_ profiles: [URL]) {
        for profile in profiles {
            if let pid = Graft.processIdentifier(of: profile) {
                DispatchQueue.main.sync {
                    _ = NSRunningApplication(processIdentifier: pid)?.terminate()
                }
            }
        }
        let deadline = Date().addingTimeInterval(20)
        while Date() < deadline, profiles.contains(where: { Graft.isRunning(profile: $0) }) {
            Thread.sleep(forTimeInterval: 0.5)
        }
    }

    /// The press, from anywhere: merges, and when a Claude is in the way asks
    /// whether to quit them, merge, and reopen them.
    @MainActor
    func mergeAsking(_ store: ShortcutStore) {
        merge(store, quitting: false) { [weak self] blocked in
            guard let self, !blocked.isEmpty else { return }
            let alert = NSAlert()
            alert.messageText = L10n.text("Quit Claude to merge chats?")
            alert.informativeText = L10n.format("""
                %@ is open. Claude builds its sidebar as it starts and rewrites \
                chat records while it runs, so chats are only merged while every \
                Claude is closed.

                Graft can quit them, merge, and open them again.
                """, blocked.map(store.name(ofProfile:)).joined(separator: ", "))
            alert.addButton(withTitle: L10n.text("Quit, Merge and Reopen"))
            alert.addButton(withTitle: L10n.text("Cancel"))
            NSApp.activate(ignoringOtherApps: true)
            if alert.runModal() == .alertFirstButtonReturn {
                self.merge(store, quitting: true)
            }
        }
    }
}
