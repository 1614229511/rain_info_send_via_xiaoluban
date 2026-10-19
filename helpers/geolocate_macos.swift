// macOS native geolocation helper (Level 2, best effort).
// Run with: swift helpers/geolocate_macos.swift
// Prints one line of JSON. CoreLocation on a bare command-line tool has no
// bundled Info.plist, so macOS may deny or never show the permission prompt.
// For reliable results install CoreLocationCLI (brew install corelocationcli);
// rain_monitor.py prefers it automatically.
import CoreLocation
import Foundation

final class Locator: NSObject, CLLocationManagerDelegate {
    private let manager = CLLocationManager()
    private(set) var finished = false

    func start() {
        manager.delegate = self
        manager.desiredAccuracy = kCLLocationAccuracyHundredMeters
        manager.requestWhenInUseAuthorization()
        manager.startUpdatingLocation()
    }

    func finish(_ payload: [String: Any]) {
        guard !finished else { return }
        finished = true
        manager.stopUpdatingLocation()
        if let data = try? JSONSerialization.data(withJSONObject: payload, options: []),
           let text = String(data: data, encoding: .utf8) {
            print(text)
        } else {
            print("{\"ok\":false,\"error\":\"failed to encode result\",\"source\":\"macos_corelocation\"}")
        }
        CFRunLoopStop(CFRunLoopGetMain())
    }

    func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard let location = locations.last else { return }
        finish([
            "ok": true,
            "latitude": location.coordinate.latitude,
            "longitude": location.coordinate.longitude,
            "accuracy": location.horizontalAccuracy,
            "timestamp": ISO8601DateFormatter().string(from: location.timestamp),
            "source": "macos_corelocation",
        ])
    }

    func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        finish(["ok": false, "error": error.localizedDescription, "source": "macos_corelocation"])
    }

    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        switch manager.authorizationStatus {
        case .authorizedAlways, .authorizedWhenInUse:
            manager.startUpdatingLocation()
        case .denied, .restricted:
            finish(["ok": false, "error": "location access denied", "source": "macos_corelocation"])
        default:
            break
        }
    }
}

let locator = Locator()
locator.start()
CFRunLoopRunInMode(.defaultMode, 12.0, false)
if !locator.isFinished {
    locator.finish(["ok": false, "error": "timeout", "source": "macos_corelocation"])
}
