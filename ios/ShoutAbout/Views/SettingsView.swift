import SwiftUI
import UIKit

/// Settings (a sheet from the main screen): the computers, the Action Button guide, a couple
/// of plain switches. Everything technical lives under Advanced.
struct SettingsView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var showConnect = false
    @State private var removing: ComputerChoice?
    @State private var renaming: ComputerRename?

    var body: some View {
        @Bindable var model = model
        NavigationStack {
            Form {
                computersSection
                Section {
                    NavigationLink {
                        ScrollView {
                            VStack(spacing: Theme.Spacing.xl) {
                                ActionButtonGuide()
                                ControlCenterGuide()
                            }
                            .padding(Theme.Spacing.xl)
                        }
                        .background(Theme.Colors.ground.ignoresSafeArea())
                        .navigationBarTitleDisplayMode(.inline)
                    } label: {
                        Label("One press to talk", systemImage: "button.horizontal.top.press")
                    }
                    KeyboardSetupRow()
                } footer: {
                    if !model.keyboardOn {
                        Text("Keyboards › ShoutAbout, then Allow Full Access.")
                    }
                }
                Section {
                    if model.hasComputer {
                        Toggle("Also copy on this phone", isOn: $model.copyInPCMode)
                        NavigationLink {
                            VocabView()
                        } label: {
                            LabeledContent("Custom words", value: "\(model.vocab.count)")
                        }
                    }
                    Toggle("Loud red app icon", isOn: Binding(
                        get: { model.loudIcon },
                        set: { newValue in Task { await model.setLoudIcon(newValue) } }
                    ))
                } header: {
                    Text("Dictation")
                }
                Section {
                    NavigationLink("Advanced") { AdvancedSettingsView() }
                }
                Section {
                    LabeledContent("Version", value: Self.version)
                    NavigationLink("Font licenses") { LicensesView() }
                } header: {
                    Text("About")
                }
            }
            .scrollContentBackground(.hidden)
            .background(Theme.Colors.ground.ignoresSafeArea())
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                        .font(Theme.Typography.bold(17))
                }
            }
            .sheet(isPresented: $showConnect) {
                NavigationStack {
                    ConnectComputerView(onConnected: { showConnect = false }, onSkip: nil)
                        .background(Theme.Colors.ground.ignoresSafeArea())
                        .toolbar {
                            ToolbarItem(placement: .cancellationAction) {
                                Button("Close") { showConnect = false }
                            }
                        }
                }
            }
            .confirmationDialog(
                "Remove \(removing?.name ?? "this computer")?",
                isPresented: Binding(get: { removing != nil }, set: { if !$0 { removing = nil } }),
                titleVisibility: .visible,
                presenting: removing
            ) { choice in
                Button("Remove", role: .destructive) { model.removeComputer(choice.id) }
            }
            .renameComputerAlert($renaming)
            .task {
                model.syncIconState()
            }
        }
        .tint(Theme.Colors.tint)
    }

    /// Each computer (a tick on the selected one: tap to choose, swipe or long-press to rename
    /// or remove), the connection check for the selected one, and "Add a computer".
    @ViewBuilder
    private var computersSection: some View {
        let choices = model.computerChoices
        Section {
            ForEach(choices) { choice in
                computerRow(choice)
            }
            if model.hasComputer {
                Button {
                    Task { await model.testConnection() }
                } label: {
                    HStack {
                        Label("Check connection", systemImage: "antenna.radiowaves.left.and.right")
                        if model.isCheckingConnection {
                            Spacer()
                            ProgressView()
                        }
                    }
                }
                .disabled(model.isCheckingConnection)
                ForEach(model.connectionChecks) { check in
                    HStack(alignment: .top) {
                        Image(systemName: check.ok ? "checkmark.circle.fill" : "xmark.octagon.fill")
                            .foregroundStyle(check.ok ? Color.green : Theme.Palette.tongueRed)
                        VStack(alignment: .leading) {
                            Text(check.title).font(Theme.Typography.bold(15))
                            Text(check.detail)
                                .font(Theme.Typography.body(13))
                                .foregroundStyle(.secondary)
                        }
                    }
                }
            }
            Button {
                showConnect = true
            } label: {
                Label(choices.isEmpty ? "Connect a computer" : "Add a computer", systemImage: choices.isEmpty ? "desktopcomputer" : "plus")
                    .font(Theme.Typography.bold(17))
            }
        } header: {
            Text(choices.count > 1 ? "Your computers" : "Your computer")
        }
    }

    private func computerRow(_ choice: ComputerChoice) -> some View {
        let selected = choice.id == model.selectedChoiceID
        return Button {
            if choice.usable {
                model.selectComputer(choice.id)
            } else {
                showConnect = true  // no token on this iPhone any more: pair it again
            }
        } label: {
            HStack(spacing: Theme.Spacing.m) {
                Image(systemName: "desktopcomputer")
                    .font(.system(size: 20, weight: .bold))
                    .foregroundStyle(Theme.Palette.shoutYellow)
                    .frame(width: 44, height: 44)
                    .background(Circle().fill(Theme.Palette.black))
                VStack(alignment: .leading, spacing: 2) {
                    Text(choice.name)
                        .font(Theme.Typography.bold(17))
                        .foregroundStyle(.primary)
                    Text(detail(choice, selected: selected))
                        .font(Theme.Typography.body(13))
                        .foregroundStyle(.secondary)
                }
                Spacer(minLength: 0)
                if selected {
                    Image(systemName: "checkmark")
                        .font(.system(size: 17, weight: .bold))
                        .foregroundStyle(Theme.Colors.tint)
                        .accessibilityHidden(true)
                }
            }
        }
        .accessibilityLabel(choice.name)
        .accessibilityValue(detail(choice, selected: selected))
        .accessibilityAddTraits(selected ? .isSelected : [])
        .swipeActions(edge: .trailing, allowsFullSwipe: false) {
            // Not role .destructive: that animates the row away before the confirmation.
            Button("Remove") { removing = choice }
                .tint(Theme.Palette.tongueRed)
            if !choice.isCodePairing {
                Button("Rename") { renaming = ComputerRename(id: choice.id, text: choice.name) }
            }
        }
        .contextMenu {
            if !choice.isCodePairing {
                Button("Rename", systemImage: "pencil") { renaming = ComputerRename(id: choice.id, text: choice.name) }
            }
            Button("Remove", systemImage: "trash", role: .destructive) { removing = choice }
        }
    }

    /// "DESKTOP-Q0DB401 · Fast": the PC's own name when it's shown shorter (or renamed), and
    /// for the selected one how it's doing.
    private func detail(_ choice: ComputerChoice, selected: Bool) -> String {
        var parts: [String] = []
        if !choice.reportedName.isEmpty, choice.reportedName != choice.name { parts.append(choice.reportedName) }
        if !choice.usable {
            parts.append("Pair again")
        } else if selected {
            parts.append(statusLine)
        }
        return parts.isEmpty ? "Paired" : parts.joined(separator: " · ")
    }

    private var statusLine: String {
        var parts: [String] = []
        if let info = model.hubInfo?.info {
            switch info.speed {
            case .fast: parts.append("Fast")
            case .ok: parts.append("Ready")
            case .slow: parts.append("Ready")
            }
        } else {
            parts.append("Paired")
        }
        if model.computerLink()?.tunnel != nil { parts.append("tunnel when away") }
        return parts.joined(separator: " · ")
    }

    static var version: String {
        let info = Bundle.main.infoDictionary
        let short = info?["CFBundleShortVersionString"] as? String ?? "?"
        let build = info?["CFBundleVersion"] as? String ?? "?"
        return "\(short) (\(build))"
    }
}

/// A computer being renamed: its id and the name typed so far.
struct ComputerRename: Identifiable, Equatable {
    var id: String
    var text: String
}

extension View {
    /// Renames a computer on this iPhone (Settings, a long-press on the main screen's switch).
    func renameComputerAlert(_ rename: Binding<ComputerRename?>) -> some View {
        modifier(RenameComputerAlert(rename: rename))
    }
}

private struct RenameComputerAlert: ViewModifier {
    @Environment(AppModel.self) private var model
    @Binding var rename: ComputerRename?

    func body(content: Content) -> some View {
        content.alert("Rename", isPresented: Binding(
            get: { rename != nil },
            set: { if !$0 { rename = nil } }
        )) {
            TextField("Desktop", text: Binding(
                get: { rename?.text ?? "" },
                set: { newValue in
                    if var current = rename {
                        current.text = newValue
                        rename = current
                    }
                }
            ))
            .textInputAutocapitalization(.words)
            Button("Save") {
                if let current = rename { model.renameComputer(current.id, to: current.text) }
                rename = nil
            }
            Button("Cancel", role: .cancel) { rename = nil }
        } message: {
            Text("Only on this iPhone. Empty uses the PC's own name.")
        }
    }
}

/// Settings › Keyboard: "On" once the ShoutAbout keyboard has come up with Full Access;
/// otherwise a button into ShoutAbout's page in the Settings app, where iOS lists its keyboard.
private struct KeyboardSetupRow: View {
    @Environment(AppModel.self) private var model
    @Environment(\.openURL) private var openURL

    var body: some View {
        if model.keyboardOn {
            LabeledContent {
                Text("On")
            } label: {
                Label("Keyboard", systemImage: "keyboard")
            }
        } else {
            Button {
                if let url = URL(string: UIApplication.openSettingsURLString) {
                    openURL(url)
                }
            } label: {
                LabeledContent {
                    Text("Turn on")
                        .font(Theme.Typography.bold(15))
                } label: {
                    Label("Keyboard", systemImage: "keyboard")
                }
            }
        }
    }
}

/// Advanced: the on-device engine, the older pairing methods, the owner's Cloudflare route.
struct AdvancedSettingsView: View {
    @Environment(AppModel.self) private var model
    @State private var showScanner = false
    @State private var confirmForgetLegacy = false

    var body: some View {
        @Bindable var model = model
        Form {
            Section {
                Picker("Speech engine", selection: $model.offlineEngine) {
                    ForEach(OfflineEnginePreference.allCases) { engine in
                        Text(engine.label).tag(engine)
                    }
                }
                LabeledContent("Model", value: model.offlineModelStatus.isEmpty ? "…" : model.offlineModelStatus)
                Button {
                    Task { await model.prepareOffline() }
                } label: {
                    HStack {
                        Label("Download the model", systemImage: "arrow.down.circle")
                        if model.isPreparingOffline {
                            Spacer()
                            ProgressView()
                        }
                    }
                }
                .disabled(model.isPreparingOffline)
            } header: {
                Text("On this iPhone")
            } footer: {
                Text("Download ahead to dictate without a network.")
            }

            Section {
                Button {
                    showScanner = true
                } label: {
                    Label("Scan a pairing code", systemImage: "qrcode.viewfinder")
                }
                NavigationLink {
                    ManualPairingView()
                } label: {
                    Label("Enter pairing details", systemImage: "keyboard")
                }
                if model.hasLegacyPairing {
                    LabeledContent("Home address", value: model.configuration.lanURL?.absoluteString ?? "—")
                    if model.configuration.publicURL != nil {
                        LabeledContent("Public URL", value: model.configuration.publicURL?.absoluteString ?? "—")
                        Toggle("Use my tunnel when away", isOn: $model.useTunnel)
                    }
                    Button("Forget the code pairing", role: .destructive) { confirmForgetLegacy = true }
                }
            } header: {
                Text("Pairing with a code (older PCs)")
            } footer: {
                Text("On the PC, run python -m shoutabout pair, then scan the code.")
            }

            Section {
                Button("Show the setup again") { model.showOnboardingAgain() }
            }
        }
        .navigationTitle("Advanced")
        .navigationBarTitleDisplayMode(.inline)
        .sheet(isPresented: $showScanner) {
            PairingScannerSheet()
        }
        .confirmationDialog("Forget the code pairing?", isPresented: $confirmForgetLegacy, titleVisibility: .visible) {
            Button("Forget", role: .destructive) { model.forgetLegacyPairing() }
        } message: {
            Text("Removes its tokens and addresses from this iPhone.")
        }
        .task {
            await model.refreshOfflineStatus()
        }
    }
}

private struct LicensesView: View {
    var body: some View {
        List {
            Section("Anton — SIL Open Font License 1.1") {
                Text(Self.load("OFL-Anton"))
                    .font(.footnote.monospaced())
            }
            Section("Archivo — SIL Open Font License 1.1") {
                Text(Self.load("OFL-Archivo"))
                    .font(.footnote.monospaced())
            }
        }
        .navigationTitle("Font licenses")
    }

    private static func load(_ name: String) -> String {
        guard let url = Bundle.main.url(forResource: name, withExtension: "txt"),
              let text = try? String(contentsOf: url, encoding: .utf8)
        else { return "License file missing from the bundle." }
        return text
    }
}
