// swift-tools-version: 5.10
// The neo Mac app: a menu bar app that runs neo's dashboard on this Mac and shows it in a window.
// Build the .app with scripts/build-app.sh (it bundles Python and neo's code next to this binary).
import PackageDescription

let package = Package(
    name: "Neo",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(name: "Neo", path: "Sources/Neo"),
        .testTarget(name: "NeoTests", dependencies: ["Neo"], path: "Tests/NeoTests"),
    ]
)
