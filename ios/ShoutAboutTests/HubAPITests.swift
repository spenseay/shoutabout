import XCTest

final class HubAPITests: XCTestCase {
    private func data(_ text: String) -> Data { Data(text.utf8) }

    func testDecodesFullTranscribeResponse() throws {
        let body = data("""
        {"text": "Push the release to Friday.", "raw_text": "push the release to friday", "target": "pc",
         "pasted": true, "warning": null, "status": "ok", "route": "lan", "cleanup": "ok",
         "timings_ms": {"decode": 12, "stt": 145.5, "cleanup": 91, "paste": 48, "total": 300}}
        """)
        let result = HubResponseParser.parseTranscribe(status: 200, body: body, contentType: "application/json")
        let response = try result.get()
        XCTAssertEqual(response.text, "Push the release to Friday.")
        XCTAssertEqual(response.rawText, "push the release to friday")
        XCTAssertEqual(response.pasted, true)
        XCTAssertNil(response.warning)
        XCTAssertEqual(response.status, "ok")
        XCTAssertEqual(response.hubRoute, .lan)
        XCTAssertEqual(response.timingsMs?.stt, 145.5)
        XCTAssertEqual(response.timingsMs?.total, 300)
    }

    func testDecodesOlderHubWithoutStatusAndWithWarning() throws {
        let body = data(#"{"text": "", "target": "phone", "pasted": false, "warning": "No speech detected.", "route": "tunnel"}"#)
        let response = try HubResponseParser.parseTranscribe(status: 200, body: body, contentType: nil).get()
        XCTAssertEqual(response.text, "")
        XCTAssertEqual(response.warning, "No speech detected.")
        XCTAssertNil(response.status)
        XCTAssertNil(response.timingsMs)
        XCTAssertEqual(response.hubRoute, .tunnel)
    }

    func testHubJSONError() {
        let body = data(#"{"detail": "missing or invalid bearer token"}"#)
        let result = HubResponseParser.parseTranscribe(status: 401, body: body, contentType: "application/json")
        XCTAssertEqual(result.failure, .hub(status: 401, detail: "missing or invalid bearer token"))
    }

    func testCloudflareHTMLIsGatewayFailure() {
        let body = data("<!DOCTYPE html><html><head><title>Forbidden</title></head><body>Access denied</body></html>")
        let result = HubResponseParser.parseTranscribe(status: 401, body: body, contentType: "text/html")
        guard case .gateway(let status, let snippet)? = result.failure else {
            return XCTFail("expected gateway, got \(String(describing: result.failure))")
        }
        XCTAssertEqual(status, 401)
        XCTAssertTrue(snippet.hasPrefix("<!DOCTYPE html>"))
        XCTAssertTrue(HubFailure.gateway(status: 401, snippet: "").userMessage.contains("Cloudflare Access"))
    }

    func testBadSuccessBody() {
        let result = HubResponseParser.parseTranscribe(status: 200, body: data("<html>"), contentType: "text/html")
        XCTAssertEqual(result.failure, .badResponse("not JSON"))
        let wrongShape = HubResponseParser.parseTranscribe(status: 200, body: data(#"{"ok": true}"#), contentType: nil)
        XCTAssertEqual(wrongShape.failure, .badResponse("unexpected JSON"))
    }

    func testValidationErrorListDetail() {
        let body = data(#"{"detail": [{"loc": ["query", "target"], "msg": "bad"}]}"#)
        guard case .hub(status: 422, detail: let detail)? = HubResponseParser.parseTranscribe(status: 422, body: body, contentType: nil).failure else {
            return XCTFail("expected hub error")
        }
        XCTAssertTrue(detail.contains("bad"))
    }

    func testLegacyMultipartDetection() {
        XCTAssertTrue(HubResponseParser.isLegacyMultipartOnly(.hub(status: 400, detail: "missing 'file' upload")))
        XCTAssertFalse(HubResponseParser.isLegacyMultipartOnly(.hub(status: 400, detail: "empty upload")))
        XCTAssertFalse(HubResponseParser.isLegacyMultipartOnly(.hub(status: 401, detail: "missing 'file' upload")))
    }

    func testVocabDecoding() throws {
        let list = try HubResponseParser.parse(
            VocabList.self, status: 200, body: data(#"{"terms": ["Better Shell", "ShoutAbout"], "version": 3}"#), contentType: nil
        ).get()
        XCTAssertEqual(list.terms, ["Better Shell", "ShoutAbout"])
        XCTAssertEqual(list.version, 3)
        XCTAssertNil(list.replacements)  // a PC from before replacements
        let withReplacements = try HubResponseParser.parse(
            VocabList.self,
            status: 200,
            body: data(#"{"terms": ["Sofia"], "replacements": [{"say": "omw", "write": "on my way"}], "version": 4}"#),
            contentType: nil
        ).get()
        XCTAssertEqual(withReplacements.replacements, [VocabReplacement(say: "omw", write: "on my way")])
        let added = try HubResponseParser.parse(
            VocabAddResult.self, status: 200, body: data(#"{"added": true, "term": "Parakeet", "count": 3}"#), contentType: nil
        ).get()
        XCTAssertEqual(added, VocabAddResult(added: true, term: "Parakeet", count: 3))
    }

    func testTranscribeURL() {
        let base = URL(string: "http://192.168.1.50:8765")!
        XCTAssertEqual(HubURL.transcribe(base, target: .phone).absoluteString, "http://192.168.1.50:8765/transcribe?target=phone")
        let prefixed = URL(string: "https://example.com/hub")!
        XCTAssertEqual(HubURL.endpoint(prefixed, path: "health").absoluteString, "https://example.com/hub/health")
    }

    func testMultipartBody() {
        let body = MultipartBody.make(boundary: "B", target: .pc, audio: Data("AUDIO".utf8))
        let text = String(decoding: body, as: UTF8.self)
        XCTAssertTrue(text.hasPrefix("--B\r\nContent-Disposition: form-data; name=\"target\"\r\n\r\npc\r\n"))
        XCTAssertTrue(text.contains("name=\"file\"; filename=\"dictation.m4a\"\r\nContent-Type: audio/x-m4a\r\n\r\nAUDIO\r\n"))
        XCTAssertTrue(text.hasSuffix("--B--\r\n"))
        XCTAssertEqual(MultipartBody.contentType(boundary: "B"), "multipart/form-data; boundary=B")
    }
}

private extension Result {
    var failure: Failure? {
        if case .failure(let error) = self { return error }
        return nil
    }
}
