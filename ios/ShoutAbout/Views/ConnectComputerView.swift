import Observation
import SwiftUI
import UIKit

/// Find the PC on the Wi-Fi, tap it, click Allow on the PC. No QR code, no typing.
/// Used by onboarding and by Settings › Connect a computer.
struct ConnectComputerView: View {
    @Environment(AppModel.self) private var model
    @State private var pairing = PairingModel.makeForCurrentScene()
    @State private var showScanner = false
    @State private var showManual = false
    @State private var showMore = false
    @State private var address = ""
    var onConnected: () -> Void
    var onSkip: (() -> Void)?

    var body: some View {
        Group {
            switch pairing.step {
            case .searching:
                searching
            case .contacting(let name):
                busy(title: "Saying hi", detail: "Knocking on \(name)…")
            case .waiting(let code, let name):
                waiting(code: code, name: name)
            case .connected(let name):
                connected(name: name)
            case .problem(let message):
                problem(message)
            }
        }
        .animation(.spring(duration: 0.35), value: pairing.step)
        .onAppear { pairing.start() }
        .onDisappear { pairing.stop() }
        .sheet(isPresented: $showScanner, onDismiss: legacyPairingMaybeDone) {
            PairingScannerSheet()
        }
        .sheet(isPresented: $showManual, onDismiss: legacyPairingMaybeDone) {
            NavigationStack {
                ManualPairingView()
                    .toolbar {
                        ToolbarItem(placement: .cancellationAction) {
                            Button("Close") { showManual = false }
                        }
                    }
            }
        }
    }

    /// The QR code or the manual form paired the old way.
    private func legacyPairingMaybeDone() {
        if model.hasComputer, case .searching = pairing.step {
            pairing.markConnected(model.computerDisplayName)
        }
    }

    // MARK: Searching

    private var searching: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Spacing.l) {
                Text("Find your computer")
                    .shoutHeadline(40)
                    .padding(.top, Theme.Spacing.huge + Theme.Spacing.xl)
                Text("Open ShoutAbout on your PC, on the same Wi-Fi.")
                    .font(Theme.Typography.body(16))
                    .foregroundStyle(Theme.Colors.inkMuted)
                    .fixedSize(horizontal: false, vertical: true)

                if pairing.finder.state == .needsPermission {
                    permissionCard
                }

                VStack(spacing: Theme.Spacing.m) {
                    ForEach(pairing.finder.computers) { computer in
                        ComputerRow(computer: computer) {
                            pairing.connect(computer, model: model)
                        }
                    }
                    if pairing.finder.computers.isEmpty, pairing.finder.state != .needsPermission {
                        LookingRow()
                    }
                }

                DisclosureGroup(isExpanded: $showMore) {
                    VStack(alignment: .leading, spacing: Theme.Spacing.m) {
                        Button("Scan a code instead") { showScanner = true }
                            .font(Theme.Typography.bold(15))
                        Text("Or type the address your PC shows:")
                            .font(Theme.Typography.body(14))
                            .foregroundStyle(Theme.Colors.inkMuted)
                        HStack {
                            TextField("192.168.1.20:8766", text: $address)
                                .keyboardType(.URL)
                                .textInputAutocapitalization(.never)
                                .autocorrectionDisabled()
                                .textFieldStyle(.roundedBorder)
                            Button("Connect") { pairing.connect(address: address, model: model) }
                                .font(Theme.Typography.bold(16))
                                .disabled(address.trimmingCharacters(in: .whitespaces).isEmpty)
                        }
                        Button("Enter pairing details (older PCs)") { showManual = true }
                            .font(Theme.Typography.bold(15))
                    }
                    .padding(.top, Theme.Spacing.s)
                } label: {
                    Text("More options")
                        .font(Theme.Typography.bold(16))
                        .foregroundStyle(Theme.Colors.ink)
                }
                .tint(Theme.Colors.ink)

                if let onSkip {
                    Button("Not now", action: onSkip)
                        .font(Theme.Typography.bold(16))
                        .foregroundStyle(Theme.Colors.ink)
                        .frame(maxWidth: .infinity)
                        .padding(.top, Theme.Spacing.s)
                }
            }
            .padding(.horizontal, Theme.Spacing.xl)
            .padding(.bottom, Theme.Spacing.xl)
        }
        .scrollIndicators(.hidden)
    }

    private var permissionCard: some View {
        InfoCard(
            systemImage: "wifi.exclamationmark",
            title: "Allow Local Network",
            detail: "Turn on Settings › ShoutAbout › Local Network."
        ) {
            Button("Open Settings") {
                if let url = URL(string: UIApplication.openSettingsURLString) {
                    UIApplication.shared.open(url)
                }
            }
            .font(Theme.Typography.bold(15))
        }
    }

    // MARK: Contacting / waiting / done / problem

    private func busy(title: String, detail: String) -> some View {
        VStack(spacing: Theme.Spacing.l) {
            Spacer()
            MouthTile(size: 140)
            Text(title).shoutHeadline(40)
            Text(detail)
                .font(Theme.Typography.body(16))
                .foregroundStyle(Theme.Colors.inkMuted)
                .multilineTextAlignment(.center)
            Spacer()
            Button("Cancel") { pairing.cancel() }
                .font(Theme.Typography.bold(16))
                .foregroundStyle(Theme.Colors.ink)
                .padding(.bottom, Theme.Spacing.l)
        }
        .padding(.horizontal, Theme.Spacing.xl)
    }

    private func waiting(code: String, name: String) -> some View {
        VStack(spacing: Theme.Spacing.l) {
            Spacer(minLength: Theme.Spacing.huge)
            MouthTile(size: 120)
            Text("Click Allow on your PC")
                .shoutHeadline(40)
                .multilineTextAlignment(.center)
            Text("Check the code matches on \(name).")
                .font(Theme.Typography.body(16))
                .foregroundStyle(Theme.Colors.inkMuted)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
            Text(Self.spaced(code))
                .font(Theme.Typography.display(76))
                .foregroundStyle(Theme.Colors.cardInk)
                .padding(.horizontal, Theme.Spacing.xl)
                .padding(.vertical, Theme.Spacing.s)
                .background(RoundedRectangle(cornerRadius: Theme.Radius.card, style: .continuous).fill(Theme.Colors.card))
                .overlay(
                    RoundedRectangle(cornerRadius: Theme.Radius.card, style: .continuous)
                        .stroke(Theme.Colors.ink, lineWidth: Theme.Size.borderWidth)
                )
                .accessibilityLabel("Code \(code)")
                .accessibilityIdentifier("pairingCode")
            Spacer()
            Button("Cancel") { pairing.cancel() }
                .font(Theme.Typography.bold(16))
                .foregroundStyle(Theme.Colors.ink)
                .padding(.bottom, Theme.Spacing.l)
        }
        .padding(.horizontal, Theme.Spacing.xl)
    }

    private func connected(name: String) -> some View {
        VStack(spacing: Theme.Spacing.l) {
            Spacer()
            MouthTile(size: 150, open: true)
                .shoutPunch(trigger: 1, strength: 1.4)
            Text("Connected!")
                .shoutHeadline(48)
            Text(name)
                .font(Theme.Typography.body(17))
                .foregroundStyle(Theme.Colors.inkMuted)
                .multilineTextAlignment(.center)
            Spacer()
            PrimaryButton(title: "Continue", action: onConnected)
                .padding(.bottom, Theme.Spacing.m)
        }
        .padding(.horizontal, Theme.Spacing.xl)
        .sensoryFeedback(.success, trigger: name)
    }

    private func problem(_ message: String) -> some View {
        VStack(spacing: Theme.Spacing.l) {
            Spacer()
            MouthTile(size: 130, failed: true)
            Text("Didn't connect")
                .shoutHeadline(40)
            Text(message)
                .font(Theme.Typography.body(16))
                .foregroundStyle(Theme.Colors.inkMuted)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
            Spacer()
            PrimaryButton(title: "Try again") { pairing.retry() }
            if let onSkip {
                Button("Not now", action: onSkip)
                    .font(Theme.Typography.bold(16))
                    .foregroundStyle(Theme.Colors.ink)
                    .padding(.bottom, Theme.Spacing.m)
            }
        }
        .padding(.horizontal, Theme.Spacing.xl)
    }

    /// "4821" -> "4 8 2 1", easier to compare at a glance.
    static func spaced(_ code: String) -> String {
        code.map(String.init).joined(separator: " ")
    }
}

private struct ComputerRow: View {
    var computer: DiscoveredComputer
    var action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: Theme.Spacing.l) {
                Image(systemName: "desktopcomputer")
                    .font(.system(size: 22, weight: .bold))
                    .foregroundStyle(Theme.Palette.shoutYellow)
                    .frame(width: 52, height: 52)
                    .background(Circle().fill(Theme.Palette.black))
                VStack(alignment: .leading, spacing: 2) {
                    Text(computer.name)
                        .font(Theme.Typography.bold(19))
                        .foregroundStyle(Theme.Colors.cardInk)
                        .lineLimit(1)
                    Text(computer.isCompatible ? "ShoutAbout \(computer.version ?? "")" : "Update ShoutAbout on this iPhone")
                        .font(Theme.Typography.body(13))
                        .foregroundStyle(Theme.Colors.cardInkMuted)
                }
                Spacer(minLength: 0)
                Image(systemName: "chevron.right")
                    .font(.system(size: 16, weight: .bold))
                    .foregroundStyle(Theme.Colors.cardInk)
            }
            .padding(Theme.Spacing.l)
            .background(RoundedRectangle(cornerRadius: Theme.Radius.card, style: .continuous).fill(Theme.Colors.card))
            .overlay(
                RoundedRectangle(cornerRadius: Theme.Radius.card, style: .continuous)
                    .stroke(Theme.Colors.ink, lineWidth: Theme.Size.borderWidth)
            )
            .contentShape(RoundedRectangle(cornerRadius: Theme.Radius.card, style: .continuous))
        }
        .buttonStyle(SquishButtonStyle())
        .disabled(!computer.isCompatible)
        .accessibilityIdentifier("computer-\(computer.name)")
    }
}

private struct LookingRow: View {
    @Environment(\.colorScheme) private var colorScheme

    var body: some View {
        HStack(spacing: Theme.Spacing.l) {
            MouthTile(size: 52)
            Text("Looking for computers…")
                .font(Theme.Typography.bold(17))
                .foregroundStyle(Theme.Colors.inkMuted)
            Spacer(minLength: 0)
        }
        .padding(Theme.Spacing.l)
        .overlay(
            RoundedRectangle(cornerRadius: Theme.Radius.card, style: .continuous)
                .stroke(Theme.Colors.inkMuted, style: StrokeStyle(lineWidth: 2, dash: [6, 6]))
        )
    }
}

struct InfoCard<Actions: View>: View {
    var systemImage: String
    var title: String
    var detail: String
    @ViewBuilder var actions: () -> Actions

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            Label(title, systemImage: systemImage)
                .font(Theme.Typography.bold(17))
                .foregroundStyle(Theme.Colors.cardInk)
            Text(detail)
                .font(Theme.Typography.body(14))
                .foregroundStyle(Theme.Colors.cardInkMuted)
                .fixedSize(horizontal: false, vertical: true)
            actions()
                .tint(Theme.Colors.cardInk)
        }
        .padding(Theme.Spacing.l)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: Theme.Radius.card, style: .continuous).fill(Theme.Colors.card))
    }
}

/// The pairing flow's state: discovery, then /hello -> /pair/request -> poll /pair/status
/// (`DevicePairing`, unit-tested in Shared/Core).
@MainActor
@Observable
final class PairingModel {
    enum Step: Equatable {
        case searching
        case contacting(String)
        case waiting(code: String, name: String)
        case connected(String)
        case problem(String)
    }

    private(set) var step: Step = .searching
    let finder = ComputerFinder()

    @ObservationIgnored private var task: Task<Void, Never>?
    @ObservationIgnored private var isDemo = false

    func start() {
        guard !isDemo else { return }
        finder.start()
    }

    func stop() {
        task?.cancel()
        task = nil
        finder.stop()
    }

    func connect(_ computer: DiscoveredComputer, model: AppModel) {
        guard !isDemo else { return }
        task?.cancel()
        step = .contacting(computer.name)
        task = Task { [weak self] in
            guard let self else { return }
            guard let base = await self.finder.baseURL(for: computer) else {
                self.step = .problem("Couldn't find \(computer.name). Is it still on?")
                return
            }
            await self.pair(base: base, name: computer.name, model: model)
        }
    }

    func connect(address: String, model: AppModel) {
        guard !isDemo else { return }
        guard let base = URLRules.lanBaseURL(address) else {
            step = .problem("Type a home address, like 192.168.1.20:8766.")
            return
        }
        task?.cancel()
        let name = base.host() ?? "your PC"
        step = .contacting(name)
        task = Task { [weak self] in
            await self?.pair(base: base, name: name, model: model)
        }
    }

    func cancel() {
        task?.cancel()
        task = nil
        step = .searching
        start()
    }

    func retry() {
        step = .searching
        start()
    }

    func markConnected(_ name: String) {
        step = .connected(name)
    }

    private func pair(base: URL, name: String, model: AppModel) async {
        let pairing = DevicePairing(transport: LiveHubTransport())
        let hello: HelloResponse
        switch await pairing.hello(base) {
        case .success(let value):
            hello = value
        case .failure(let problem):
            step = .problem(problem.message)
            return
        }
        guard !Task.isCancelled else { return }
        let pcName = hello.name.nonBlank ?? name

        let accepted: PairRequestAccepted
        switch await pairing.request(base, deviceName: model.deviceName, deviceID: model.deviceID) {
        case .success(let value):
            accepted = value
        case .failure(let problem):
            step = .problem(problem.message)
            return
        }
        guard !Task.isCancelled else { return }
        step = .waiting(code: accepted.code, name: pcName)

        switch await pairing.waitForApproval(base, requestID: accepted.requestID, expiresIn: accepted.expiresIn) {
        case .approved(let token, let hub):
            do {
                try model.applyDevicePairing(token: token, hub: hub, address: base, fallbackName: pcName)
                step = .connected(model.computerName ?? pcName)
            } catch {
                step = .problem("Couldn't save the connection: \(error.localizedDescription)")
            }
        case .denied:
            step = .problem("\(pcName) said no. Try again and click Allow.")
        case .expired:
            step = .problem("Nobody clicked Allow in time. Try again.")
        case .cancelled:
            break
        case .failed(let message):
            step = .problem(message)
        }
    }

    static func makeForCurrentScene() -> PairingModel {
        let model = PairingModel()
#if DEBUG
        if let scene = ScreenshotScene.current {
            model.isDemo = true
            if scene == .pairingCode {
                model.step = .waiting(code: "4821", name: ScreenshotScene.sampleComputer.name)
            } else {
                model.finder.showDemo(ScreenshotScene.sampleComputers)
            }
        }
#endif
        return model
    }
}
