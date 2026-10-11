import SwiftUI
import UIKit
import VisionKit

/// Sheet: scan the pairing QR from `python -m shoutabout pair`, or paste the code.
struct PairingScannerSheet: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var message: String?
    @State private var pendingToken: String?
    @State private var paired = 0
    @State private var handled = false

    var body: some View {
        NavigationStack {
            VStack(spacing: Theme.Spacing.l) {
                if QRScannerView.isAvailable {
                    QRScannerView { payload in handle(payload) }
                        .clipShape(RoundedRectangle(cornerRadius: Theme.Radius.card, style: .continuous))
                        .overlay(
                            RoundedRectangle(cornerRadius: Theme.Radius.card, style: .continuous)
                                .stroke(Theme.Palette.shoutYellow, lineWidth: 4)
                        )
                } else {
                    ContentUnavailableView(
                        "Camera scanning unavailable",
                        systemImage: "camera.metering.unknown",
                        description: Text("Allow camera access in Settings, or copy the pairing code on the PC and use Paste below.")
                    )
                }
                if let message {
                    Text(message)
                        .font(Theme.Typography.body(15))
                        .foregroundStyle(Theme.Palette.tongueRed)
                        .multilineTextAlignment(.center)
                }
                Button {
                    handle(UIPasteboard.general.string ?? "")
                } label: {
                    Label("Paste pairing code", systemImage: "doc.on.clipboard")
                        .font(Theme.Typography.bold(16))
                }
                .buttonStyle(.bordered)
            }
            .padding(Theme.Spacing.l)
            .navigationTitle("Pair with your PC")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Close") { dismiss() }
                }
            }
            .alert("Use as hub token?", isPresented: Binding(
                get: { pendingToken != nil },
                set: { if !$0 { pendingToken = nil } }
            )) {
                Button("Use token") {
                    if let token = pendingToken {
                        do {
                            try model.applyToken(token)
                            dismiss()
                        } catch {
                            message = error.localizedDescription
                        }
                    }
                    pendingToken = nil
                }
                Button("Cancel", role: .cancel) {
                    pendingToken = nil
                    handled = false
                }
            } message: {
                Text("This QR only holds a hub token (from `token --qr`). The URLs stay as they are. For full pairing run `python -m shoutabout pair` on the PC.")
            }
        }
        .sensoryFeedback(.success, trigger: paired)
    }

    private func handle(_ text: String) {
        guard !handled else { return }
        do {
            let payload = try PairingPayload.parse(text)
            handled = true
            try model.applyPairing(payload)
            paired += 1
            dismiss()
        } catch let error as PairingError {
            if error == .notJSON, PairingPayload.looksLikeBareToken(text) {
                handled = true
                pendingToken = text.trimmingCharacters(in: .whitespacesAndNewlines)
            } else {
                message = error.localizedDescription
            }
        } catch {
            handled = false
            message = error.localizedDescription
        }
    }
}

/// VisionKit's DataScannerViewController, QR codes only.
struct QRScannerView: UIViewControllerRepresentable {
    var onScan: (String) -> Void

    static var isAvailable: Bool {
        DataScannerViewController.isSupported && DataScannerViewController.isAvailable
    }

    func makeUIViewController(context: Context) -> DataScannerViewController {
        let scanner = DataScannerViewController(
            recognizedDataTypes: [.barcode(symbologies: [.qr])],
            qualityLevel: .balanced,
            recognizesMultipleItems: false,
            isHighFrameRateTrackingEnabled: false,
            isPinchToZoomEnabled: true,
            isGuidanceEnabled: true,
            isHighlightingEnabled: true
        )
        scanner.delegate = context.coordinator
        return scanner
    }

    func updateUIViewController(_ scanner: DataScannerViewController, context: Context) {
        context.coordinator.onScan = onScan
        if !scanner.isScanning {
            try? scanner.startScanning()
        }
    }

    static func dismantleUIViewController(_ scanner: DataScannerViewController, coordinator: Coordinator) {
        scanner.stopScanning()
    }

    func makeCoordinator() -> Coordinator { Coordinator(onScan: onScan) }

    @MainActor
    final class Coordinator: NSObject, DataScannerViewControllerDelegate {
        var onScan: (String) -> Void

        init(onScan: @escaping (String) -> Void) {
            self.onScan = onScan
        }

        func dataScanner(_ dataScanner: DataScannerViewController, didAdd addedItems: [RecognizedItem], allItems: [RecognizedItem]) {
            for item in addedItems {
                if case .barcode(let barcode) = item, let payload = barcode.payloadStringValue {
                    onScan(payload)
                    return
                }
            }
        }

        func dataScanner(_ dataScanner: DataScannerViewController, didTapOn item: RecognizedItem) {
            if case .barcode(let barcode) = item, let payload = barcode.payloadStringValue {
                onScan(payload)
            }
        }
    }
}

/// Manual pairing: the same fields as the QR code.
struct ManualPairingView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var lanURL = ""
    @State private var publicURL = ""
    @State private var token = ""
    @State private var cfClientID = ""
    @State private var cfClientSecret = ""
    @State private var ssid = ""
    @State private var error: String?
    @State private var loaded = false

    var body: some View {
        Form {
            Section {
                TextField("http://192.168.1.50:8765", text: $lanURL)
                    .keyboardType(.URL)
                    .textContentType(.URL)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
            } header: {
                Text("LAN URL")
            } footer: {
                Text("From `python -m shoutabout info` on the PC (lan_url). Optional if you only use the tunnel.")
            }
            Section {
                TextField("https://dictate.example.com", text: $publicURL)
                    .keyboardType(.URL)
                    .textContentType(.URL)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
            } header: {
                Text("Public URL")
            } footer: {
                Text("Your Cloudflare Tunnel hostname. Must be https.")
            }
            Section("Hub token") {
                SecureField("43 characters", text: $token)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
            }
            Section {
                TextField("Client ID (ends in .access)", text: $cfClientID)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                SecureField("Client Secret", text: $cfClientSecret)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
            } header: {
                Text("Cloudflare Access service token")
            } footer: {
                Text("Only needed for the public URL.")
            }
            Section("Home Wi-Fi (optional)") {
                TextField("Network name", text: $ssid)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
            }
            if let error {
                Section {
                    Text(error).foregroundStyle(Theme.Palette.tongueRed)
                }
            }
            Section {
                Button("Save") { save() }
                    .font(Theme.Typography.bold(17))
            }
        }
        .navigationTitle("Enter manually")
        .onAppear(perform: load)
    }

    private func load() {
        guard !loaded else { return }
        loaded = true
        lanURL = model.configuration.lanURL?.absoluteString ?? ""
        publicURL = model.configuration.publicURL?.absoluteString ?? ""
        ssid = model.configuration.homeSSID ?? ""
        if let secrets = model.currentSecrets() {
            token = secrets.token
            cfClientID = secrets.cfClientID ?? ""
            cfClientSecret = secrets.cfClientSecret ?? ""
        }
    }

    private func save() {
        do {
            let payload = try PairingPayload.make(
                lanURL: lanURL,
                publicURL: publicURL,
                token: token,
                cfClientID: cfClientID,
                cfClientSecret: cfClientSecret,
                ssid: ssid
            )
            try model.applyPairing(payload)
            dismiss()
        } catch {
            self.error = error.localizedDescription
        }
    }
}
