#if DEBUG
import SwiftUI

/// Debug screenshot scenes `keyboard-typing`, `keyboard-listening` and `keyboard-dictated`: the
/// ShoutAbout keyboard's face (Shared/UI/KeyboardFace.swift) under a made-up chat, so the
/// simulator tour can show it without turning the keyboard on in Settings.
struct KeyboardPreviewScreen: View {
    let scene: ScreenshotScene
    @State private var state = KeyboardState()

    private static let dictation = "Running about ten minutes late, save me a seat by the window."

    var body: some View {
        GeometryReader { geo in
            VStack(spacing: 0) {
                VStack(spacing: 10) {
                    Text("Sam")
                        .font(.system(size: 13))
                        .foregroundStyle(.secondary)
                        .padding(.top, geo.safeAreaInsets.top + 8)
                    Spacer()
                    HStack {
                        Text("Are you still coming tonight?")
                            .padding(.horizontal, 12)
                            .padding(.vertical, 8)
                            .background(Color(.secondarySystemBackground), in: RoundedRectangle(cornerRadius: 18))
                        Spacer(minLength: 60)
                    }
                    HStack(alignment: .bottom) {
                        Text(fieldText)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.horizontal, 12)
                            .padding(.vertical, 8)
                            .overlay(RoundedRectangle(cornerRadius: 18).stroke(Color(.separator)))
                    }
                }
                .padding(.horizontal, 12)
                .padding(.bottom, 8)
                .frame(maxHeight: .infinity)
                .background(Color(.systemBackground))

                KeyboardFace(state: state)
                    .frame(height: KeyboardLayout.height(compact: false))
                // Under the keys iOS draws its own globe on Face ID iPhones.
                KeyboardPalette.background
                    .frame(height: geo.safeAreaInsets.bottom + 34)
            }
            .ignoresSafeArea()
            .onAppear { configure(width: geo.size.width) }
        }
    }

    private var fieldText: String {
        switch scene {
        case .keyboardDictated: return "Yes! " + Self.dictation
        case .keyboardListening: return "Yes! "
        default: return "Yes! Runnin"
        }
    }

    private func configure(width: CGFloat) {
        state.hasFullAccess = true
        state.keyFrames = KeyboardLayout.frames(
            page: .letters,
            showsGlobe: false,
            width: width,
            top: state.barHeight,
            height: KeyboardLayout.height(compact: false) - state.barHeight
        )
        state.returnLabel = "send"
        state.returnIsPrimary = true
        switch scene {
        case .keyboardListening:
            state.mouth = .listening
        case .keyboardDictated:
            state.history = .undo
        default:
            state.suggestions = [
                KeyboardSuggestion(text: "Runnin", kind: .typed),
                KeyboardSuggestion(text: "Running", kind: .correction),
                KeyboardSuggestion(text: "Run in", kind: .alternative),
            ]
            state.pressed = [.character("g")]
            state.popup = .character("g")
        }
    }
}
#endif
