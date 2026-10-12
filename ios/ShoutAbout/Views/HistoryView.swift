import SwiftUI

/// Recent: the last few dictations (`HistoryStore`), to get back text that landed in the
/// wrong spot. In memory only: never saved, gone when the app quits.
struct HistoryView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            Group {
                if model.history.items.isEmpty {
                    ContentUnavailableView {
                        Label("Nothing shouted yet", systemImage: "mouth")
                    } description: {
                        Text(note)
                    }
                } else {
                    List {
                        Section {
                            ForEach(model.history.items) { item in
                                HistoryRow(item: item)
                                    .contextMenu {
                                        Button {
                                            model.copy(item)
                                        } label: {
                                            Label("Copy", systemImage: "doc.on.doc")
                                        }
                                        ShareLink(item: item.text)
                                    }
                            }
                            .onDelete { offsets in
                                let items = model.history.items
                                model.deleteHistory(Set(offsets.map { items[$0].id }))
                            }
                        } footer: {
                            Text(note)
                        }
                    }
                    .listStyle(.insetGrouped)
                    .scrollContentBackground(.hidden)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(Theme.Colors.ground.ignoresSafeArea())
            .navigationTitle("Recent")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                        .font(Theme.Typography.bold(17))
                }
            }
        }
        .tint(Theme.Colors.tint)
        .sensoryFeedback(.success, trigger: model.successTick)
    }

    private var note: LocalizedStringKey {
        "The last \(HistoryStore.limit) dictations, never saved. Gone when ShoutAbout quits."
    }
}

private struct HistoryRow: View {
    @Environment(AppModel.self) private var model
    let item: HistoryItem

    var body: some View {
        HStack(alignment: .top, spacing: Theme.Spacing.m) {
            VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                HStack(spacing: Theme.Spacing.s) {
                    Image(systemName: item.target.systemImage)
                    Text(label)
                        .font(Theme.Typography.mono(10))
                        .foregroundStyle(.secondary)
                    Spacer()
                    Text(item.date.formatted(.relative(presentation: .named, unitsStyle: .abbreviated)))
                        .font(Theme.Typography.mono(10))
                        .foregroundStyle(.secondary)
                }
                Text(item.text)
                    .font(Theme.Typography.body(16))
                    .lineLimit(4)
                if let warning = item.warning {
                    Label(warning, systemImage: "info.circle")
                        .font(Theme.Typography.body(13))
                        .foregroundStyle(.secondary)
                }
            }
            Button {
                model.copy(item)
            } label: {
                Image(systemName: "doc.on.doc")
                    .font(.system(size: 16, weight: .bold))
                    .frame(width: 44, height: 44)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.borderless)
            .accessibilityLabel("Copy")
        }
        .padding(.vertical, Theme.Spacing.xs)
    }

    private var label: String {
        item.pasted ? "PASTED" : "COPIED"
    }
}
