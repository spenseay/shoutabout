import XCTest

final class PairingPayloadTests: XCTestCase {
    func testFullPayload() throws {
        let json = """
        {"shoutabout": 1, "lan_url": "http://192.168.1.50:8765", "public_url": "https://dictate.shoutabout.fyi",
         "token": "abcDEF123_-abcDEF123_-abcDEF123_-abcDEF1234", "cf_client_id": "id.access",
         "cf_client_secret": "s3cret", "ssid": "Home Net"}
        """
        let payload = try PairingPayload.parse(json)
        XCTAssertEqual(payload.configuration.lanURL?.absoluteString, "http://192.168.1.50:8765")
        XCTAssertEqual(payload.configuration.publicURL?.absoluteString, "https://dictate.shoutabout.fyi")
        XCTAssertEqual(payload.configuration.homeSSID, "Home Net")
        XCTAssertEqual(payload.secrets.token, "abcDEF123_-abcDEF123_-abcDEF123_-abcDEF1234")
        XCTAssertEqual(payload.secrets.cfClientID, "id.access")
        XCTAssertEqual(payload.secrets.cfClientSecret, "s3cret")
        XCTAssertTrue(payload.secrets.hasCloudflareToken)
        XCTAssertTrue(payload.configuration.isPaired)
    }

    func testMinimalLanOnlyWithNulls() throws {
        let json = #"{"shoutabout":1,"lan_url":"http://10.0.0.5:8765/","public_url":null,"token":" tok ","ssid":null}"#
        let payload = try PairingPayload.parse(json)
        XCTAssertEqual(payload.configuration.lanURL?.absoluteString, "http://10.0.0.5:8765")
        XCTAssertNil(payload.configuration.publicURL)
        XCTAssertNil(payload.configuration.homeSSID)
        XCTAssertEqual(payload.secrets.token, "tok")
        XCTAssertFalse(payload.secrets.hasCloudflareToken)
    }

    func testStripsPastedEndpointPath() throws {
        let payload = try PairingPayload.make(
            lanURL: "http://192.168.1.20:8766/transcribe",
            publicURL: "https://dictate.example.com/health/",
            token: "t",
            cfClientID: nil,
            cfClientSecret: nil,
            ssid: nil
        )
        XCTAssertEqual(payload.configuration.lanURL?.absoluteString, "http://192.168.1.20:8766")
        XCTAssertEqual(payload.configuration.publicURL?.absoluteString, "https://dictate.example.com")
    }

    func testAddsDefaultSchemes() throws {
        let payload = try PairingPayload.make(
            lanURL: "192.168.1.50:8765",
            publicURL: "dictate.example.com",
            token: "t",
            cfClientID: "",
            cfClientSecret: "  ",
            ssid: ""
        )
        XCTAssertEqual(payload.configuration.lanURL?.scheme, "http")
        XCTAssertEqual(payload.configuration.publicURL?.scheme, "https")
        XCTAssertNil(payload.secrets.cfClientID)
        XCTAssertNil(payload.configuration.homeSSID)
    }

    func testRejections() {
        assertParse(#"not json"#, throws: .notJSON)
        assertParse(#"{"token":"x","lan_url":"http://192.168.0.2:1"}"#, throws: .notShoutAbout)
        assertParse(#"{"shoutabout":2,"token":"x","lan_url":"http://192.168.0.2:1"}"#, throws: .unsupportedVersion(2))
        assertParse(#"{"shoutabout":1,"lan_url":"http://192.168.0.2:1"}"#, throws: .missingToken)
        assertParse(#"{"shoutabout":1,"token":"x"}"#, throws: .noURL)
        assertParse(#"{"shoutabout":1,"token":"x","public_url":"http://dictate.example.com"}"#, throws: .insecurePublicURL)
        assertParse(#"{"shoutabout":1,"token":"x","lan_url":"http://8.8.8.8:8766"}"#, throws: .lanURLNotPrivate)
        assertParse(#"{"shoutabout":1,"token":"x","lan_url":"ftp://192.168.0.2"}"#, throws: .invalidURL(field: "LAN URL"))
        assertParse(
            #"{"shoutabout":1,"token":"x","public_url":"https://a.example.com","cf_client_id":"id"}"#,
            throws: .incompleteCloudflareToken
        )
    }

    func testHTTPSLanURLMayBePublicHost() throws {
        let payload = try PairingPayload.make(
            lanURL: "https://pc.example.com:8443",
            publicURL: nil,
            token: "t",
            cfClientID: nil,
            cfClientSecret: nil,
            ssid: nil
        )
        XCTAssertEqual(payload.configuration.lanURL?.host(), "pc.example.com")
    }

    func testBareTokenDetection() {
        XCTAssertTrue(PairingPayload.looksLikeBareToken("Xk3_9aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789-_\n"))
        XCTAssertFalse(PairingPayload.looksLikeBareToken("short"))
        XCTAssertFalse(PairingPayload.looksLikeBareToken(#"{"shoutabout":1}"#))
        XCTAssertFalse(PairingPayload.looksLikeBareToken("has spaces in it and is long enough to pass the length"))
    }

    func testPrivateHosts() {
        for host in ["10.1.2.3", "172.16.0.1", "172.31.255.255", "192.168.1.50", "169.254.1.1", "100.64.0.1",
                     "100.127.255.255", "127.0.0.1", "fd00::1", "[fe80::1]", "::1", "my-pc", "my-pc.local",
                     "pc.home.arpa", "pc.lan"] {
            XCTAssertTrue(URLRules.isPrivateHost(host), host)
        }
        for host in ["8.8.8.8", "172.32.0.1", "100.128.0.1", "192.169.0.1", "dictate.example.com", "2001:db8::1",
                     "1.2.3", "256.1.1.1", ""] {
            XCTAssertFalse(URLRules.isPrivateHost(host), host)
        }
    }

    private func assertParse(_ text: String, throws expected: PairingError, line: UInt = #line) {
        XCTAssertThrowsError(try PairingPayload.parse(text), line: line) { error in
            XCTAssertEqual(error as? PairingError, expected, line: line)
        }
    }
}
