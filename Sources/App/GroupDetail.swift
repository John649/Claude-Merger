import SwiftUI

/// A group of accounts reading one chat history: the source picker, said once
/// for several shortcuts.
struct GroupDetail: View {
    @EnvironmentObject private var store: ShortcutStore
    @ObservedObject private var merger = Shared.chatMerger
    @Binding var group: ChatGroup

    var body: some View {
        Form {
            Section {
                TextField("Name", text: $group.name)
            } header: {
                SectionHeader(title: "Group", info: Self.note)
            }

            Section {
                Toggle(isOn: Binding(
                    get: { group.includesMain },
                    set: { Shared.chatMerger.markChanged(store.setMain($0, in: group.id)) })) {
                    VStack(alignment: .leading, spacing: 1) {
                        Text("Claude")
                        Text("Installed normally")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                ForEach(store.shortcuts) { shortcut in
                    Toggle(isOn: Binding(
                        get: { shortcut.groupID == group.id },
                        set: { wanted in
                            let changed = store.join(shortcut.id, to: wanted ? group.id : nil)
                            Shared.chatMerger.markChanged(changed)
                        })) {
                        VStack(alignment: .leading, spacing: 1) {
                            Text(shortcut.name)
                            Text(caption(for: shortcut))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                    .disabled(!canJoin(shortcut))
                }
            } header: {
                Text("Members")
            } footer: {
                Text("Everyone ticked here shares one chat history: a chat started, renamed, archived or deleted in any of them shows up in all of them.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            Section {
                HStack(spacing: 8) {
                    Button(merger.merging ? L10n.text("Merging…") : L10n.text("Merge Chats Now")) {
                        merger.mergeAsking(store)
                    }
                    .disabled(merger.merging)
                    if merger.merging { ProgressView().controlSize(.small) }
                }
                if let note = merger.note {
                    Text(note)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Text(Self.mergeNote)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            } header: {
                Text("Chats")
            }
        }
        .formStyle(.grouped)
    }

    /// A shortcut in another group can still be moved here; one whose source
    /// chain would loop back through itself cannot take the hub as a source.
    private func canJoin(_ shortcut: Shortcut) -> Bool {
        if shortcut.groupID == group.id { return true }
        if case .shortcut(let hub) = group.hub, hub == shortcut.id { return true }
        return Graft.samePath(store.chatRoot(for: shortcut), store.chatRoot(ofGroup: group))
            || store.availableSources(for: shortcut).contains(group.hub)
    }

    private func caption(for shortcut: Shortcut) -> String {
        if let other = shortcut.groupID, other != group.id, let name = store.group(other)?.name {
            return L10n.format("In %@", name)
        }
        if shortcut.groupID == group.id { return L10n.text("Sharing") }
        return shortcut.source == .own
            ? L10n.text("Its own chats")
            : L10n.format("Reads %@", store.label(for: shortcut.source))
    }

    private static let note = L10n.text("""
        Every account in a group shares one Claude Code chat history. Each \
        account's existing chats are merged in when it joins, and from then on \
        changes made in any of them are carried to all of the others.

        Leaving a group keeps the chats the account has. Choose Its own chats \
        on the shortcut to stop sharing. Chats merged into the others stay there.
        """)

    private static let mergeNote = L10n.text("""
        With Merge Chats Automatically on, Graft merges whenever every Claude is \
        closed. Merging now asks to quit any Claude that is open, merges, and \
        reopens them.
        """)
}
