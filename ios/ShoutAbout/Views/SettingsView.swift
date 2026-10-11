import SwiftUI

/// Settings (a sheet from the main screen): the computer, the Action Button guide, a couple
/// of plain switches. Everything technical lives under Advanced.
struct SettingsView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var showConnect = false
    @State private var confirmDisconnect = false

    var body: some View {
        @Bindable var model = model
        NavigationStack {
            Form {
                computerSection
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
                    KeepHistoryToggle()
                } header: {
                    Text("History")
                } footer: {
                    Text("Off by default. Saved only on this phone.")
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
            .confirmationDialog("Disconnect \(model.computerDisplayName)?", isPresented: $confirmDisconnect, titleVisibility: .visible) {
                Button("Disconnect", role: .destructive) { model.disconnectComputer() }
            }
            .task {
                model.syncIconState()
            }
        }
        .tint(Theme.Colors.tint)
    }

    @ViewBuilder
    private var computerSection: some View {
        Section {
            if model.hasComputer {
                HStack(spacing: Theme.Spacing.m) {
                    Image(systemName: "desktopcomputer")
                        .font(.system(size: 20, weight: .bold))
                        .foregroundStyle(Theme.Palette.shoutYellow)
                        .frame(width: 44, height: 44)
                        .background(Circle().fill(Theme.Palette.black))
                    VStack(alignment: .leading, spacing: 2) {
                        Text(model.computerName ?? "Your PC")
                            .font(Theme.Typography.bold(17))
                        Text(statusLine)
                            .font(Theme.Typography.body(13))
                            .foregroundStyle(.secondary)
                    }
                }
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
                Button("Disconnect this computer", role: .destructive) { confirmDisconnect = true }
            } else {
                Button {
                    showConnect = true
                } label: {
                    Label("Connect a computer", systemImage: "desktopcomputer")
                        .font(Theme.Typography.bold(17))
                }
            }
        } header: {
            Text("Your computer")
        }
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

/// "Keep history" (Settings, and the History screen's empty state turns it on). Turning it off
/// with saved dictations asks whether to keep or clear them.
struct KeepHistoryToggle: View {
    @Environment(AppModel.self) private var model
    @State private var confirmOff = false

    var body: some View {
        Toggle("Keep history", isOn: Binding(
            get: { model.keepHistory },
            set: { on in
                if !on, !model.history.isEmpty {
                    confirmOff = true
                } else {
                    model.setKeepHistory(on)
                }
            }
        ))
        .confirmationDialog("Turn off history?", isPresented: $confirmOff, titleVisibility: .visible) {
            Button("Clear saved dictations", role: .destructive) { model.setKeepHistory(false, clearSaved: true) }
            Button("Keep saved dictations") { model.setKeepHistory(false) }
            Button("Cancel", role: .cancel) {}
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
