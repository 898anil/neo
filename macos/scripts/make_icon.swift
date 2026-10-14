// Draws the app icon (the dashboard's three-dot mark on a macOS squircle) into an .iconset folder.
//   swift scripts/make_icon.swift <out.iconset>   then   iconutil -c icns <out.iconset>
import AppKit

let out = URL(fileURLWithPath: CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "Neo.iconset")
try FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)

func color(_ hex: UInt32, _ alpha: CGFloat = 1) -> NSColor {
    NSColor(srgbRed: CGFloat((hex >> 16) & 0xff) / 255, green: CGFloat((hex >> 8) & 0xff) / 255,
            blue: CGFloat(hex & 0xff) / 255, alpha: alpha)
}

func draw(size: Int) -> Data {
    let s = CGFloat(size)
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size, bitsPerSample: 8,
                               samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                               bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    // Apple's grid: the shape is 824/1024 of the canvas, centred, corner radius ~185/1024.
    let inset = s * 100 / 1024
    let body = NSRect(x: inset, y: inset, width: s - 2 * inset, height: s - 2 * inset)
    let squircle = NSBezierPath(roundedRect: body, xRadius: s * 185 / 1024, yRadius: s * 185 / 1024)
    NSGradient(starting: color(0x1a1f2b), ending: color(0x0e1118))!.draw(in: squircle, angle: -90)
    color(0xffffff, 0.06).setStroke()
    squircle.lineWidth = max(1, s / 256)
    squircle.stroke()

    let pillW = body.width * 0.78, pillH = body.width * 0.30
    let pill = NSRect(x: body.midX - pillW / 2, y: body.midY - pillH / 2, width: pillW, height: pillH)
    let pillPath = NSBezierPath(roundedRect: pill, xRadius: pillH / 2, yRadius: pillH / 2)
    color(0x1b2130).setFill()
    pillPath.fill()
    color(0x2b3245).setStroke()
    pillPath.lineWidth = max(1, s * 8 / 1024)
    pillPath.stroke()

    let r = pillH * 0.27
    let gap = pillW * 0.28
    for (i, hex) in [UInt32(0xf2a93b), 0x5b9cff, 0x4fbf8f].enumerated() {
        let cx = pill.midX + CGFloat(i - 1) * gap
        if i == 0 {
            color(0xf2a93b, 0.22).setFill()
            NSBezierPath(ovalIn: NSRect(x: cx - r * 1.5, y: pill.midY - r * 1.5, width: r * 3, height: r * 3)).fill()
        }
        color(hex).setFill()
        NSBezierPath(ovalIn: NSRect(x: cx - r, y: pill.midY - r, width: r * 2, height: r * 2)).fill()
    }
    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])!
}

for base in [16, 32, 128, 256, 512] {
    try draw(size: base).write(to: out.appendingPathComponent("icon_\(base)x\(base).png"))
    try draw(size: base * 2).write(to: out.appendingPathComponent("icon_\(base)x\(base)@2x.png"))
}
