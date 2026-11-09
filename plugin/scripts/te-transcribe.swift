// te-transcribe: on-device speech-to-text for TokenEconomizer's `media=audio` (macOS 26+).
// usage: te-transcribe <audio-file> [locale]   -> the transcript on stdout, exit 0
// Apple's Speech framework (SpeechAnalyzer + SpeechTranscriber): no network, no account. Built
// once by `te media setup` (or the first prompt that names an audio file) with `swiftc`.
import AVFoundation
import Foundation
import Speech

@main struct TETranscribe {
  static func main() async {
    let args = CommandLine.arguments
    guard args.count > 1 else {
      FileHandle.standardError.write("usage: te-transcribe <audio-file> [locale]\n".data(using: .utf8)!)
      exit(2)
    }
    let url = URL(fileURLWithPath: args[1])
    let locale = Locale(identifier: args.count > 2 ? args[2] : Locale.current.identifier)
    do {
      let transcriber = SpeechTranscriber(locale: locale, preset: .transcription)
      if let req = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) {
        try await req.downloadAndInstall()
      }
      let collect = Task { () -> String in
        var out = ""
        for try await r in transcriber.results { out += String(r.text.characters) }
        return out
      }
      let file = try AVAudioFile(forReading: url)
      let analyzer = try await SpeechAnalyzer(inputAudioFile: file, modules: [transcriber],
                                              finishAfterFile: true)
      _ = analyzer
      print(try await collect.value.trimmingCharacters(in: .whitespacesAndNewlines))
    } catch {
      FileHandle.standardError.write("te-transcribe: \(error)\n".data(using: .utf8)!)
      exit(1)
    }
  }
}
