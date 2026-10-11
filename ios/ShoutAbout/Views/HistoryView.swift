import SwiftUI

/// Recent dictations (kept on this iPhone only, and only with Settings › Keep history on).
struct HistoryView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var confirmClear = false

    var body: some View {
        NavigationStack {
            Group {
                if model.history.isEmpty, !model.keepHistory {
                    ContentUnavailableView {
                        Label("History is off", systemImage: "clock.badge.xmark")
                    } description: {
                        Text("Turn it on to keep what you dictate on this phone, so you can copy it later.")
                    } actions: {
                        Button("Turn on history") { model.setKeepHistory(true) }
                            .font(Theme.Typography.bold(16))
                    }
                } else if model.history.isEmpty {
                    ContentUnavailableView {
                        Label("Nothing shouted yet", systemImage: "mouth")
                    } description: {
                        Text("Your dictations show up here. They stay on this iPhone.")
                    }
                } else {
                    List {
                        if !model.keepHistory {
                            Section {
                                Button("Turn on history") { model.setKeepHistory(true) }
                                    .font(Theme.Typography.bold(16))
                            } footer: {
                                Text("History is off: new dictations aren't saved.")
                            }
                        }
                        ForEach(model.history) { item in
                            HistoryRow(item: item)
                                .swipeActions(edge: .leading) {
                                    if !item.text.isEmpty {
                                        Button {
                                            model.copy(item)
                                        } label: {
                                            Label("Copy", systemImage: "doc.on.doc")
                                        }
                                        .tint(Theme.Palette.black)
                                    }
                                }
                                .contextMenu {
                                    if !item.text.isEmpty {
                                        Button {
                                            model.copy(item)
                                        } label: {
                                            Label("Copy", systemImage: "doc.on.doc")
                                        }
                                        ShareLink(item: item.text)
                                    }
                                }
                        }
                        .onDelete { offsets in
                            model.deleteHistory(Set(offsets.map { model.history[$0].id }))
                        }
                    }
                    .listStyle(.insetGrouped)
                    .scrollContentBackground(.hidden)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(Theme.Colors.ground.ignoresSafeArea())
            .navigationTitle("History")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    if !model.history.isEmpty {
                        Button("Clear", role: .destructive) { confirmClear = true }
                    }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                        .font(Theme.Typography.bold(17))
                }
            }
            .confirmationDialog("Clear all history on this iPhone?", isPresented: $confirmClear, titleVisibility: .visible) {
                Button("Clear History", role: .destructive) { model.clearHistory() }
            }
        }
        .tint(Theme.Colors.tint)
        .sensoryFeedback(.success, trigger: model.successTick)
    }
}

private struct HistoryRow: View {
    let item: HistoryItem

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
            HStack(spacing: Theme.Spacing.s) {
                Image(systemName: item.succeeded ? item.target.systemImage : "exclamationmark.triangle.fill")
                    .foregroundStyle(item.succeeded ? Color.primary : Theme.Palette.tongueRed)
                Text(label)
                    .font(Theme.Typography.mono(10))
                    .foregroundStyle(.secondary)
                Spacer()
                Text(item.date.formatted(.relative(presentation: .named, unitsStyle: .abbreviated)))
                    .font(Theme.Typography.mono(10))
                    .foregroundStyle(.secondary)
            }
            if let error = item.error {
                Text(error)
                    .font(Theme.Typography.body(15))
                    .foregroundStyle(Theme.Palette.tongueRed)
            } else if item.text.isEmpty {
                Text(item.warning ?? "No speech heard.")
                    .font(Theme.Typography.body(15))
                    .foregroundStyle(.secondary)
            } else {
                Text(item.text)
                    .font(Theme.Typography.body(16))
                    .lineLimit(4)
                if let warning = item.warning {
                    Label(warning, systemImage: "info.circle")
                        .font(Theme.Typography.body(13))
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(.vertical, Theme.Spacing.xs)
    }

    private var label: String {
        guard item.succeeded else { return "FAILED" }
        if item.pasted { return "PASTED ON PC" }
        return item.engine == nil ? "COPIED · PC WROTE IT" : "COPIED · IPHONE WROTE IT"
    }
}
