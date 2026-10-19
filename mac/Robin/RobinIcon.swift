//
//  RobinIcon.swift
//  Robin
//
//  The menu bar flower, after Nico Robin's Hana Hana no Mi, shaded periwinkle to purple like the app icon.
//  Not a template, so the menu bar keeps the color.
//

import AppKit

enum RobinIcon {
    /// An 18×18 pt five-petal flower; the centre is cut out around a dot.
    static func flower() -> NSImage {
        let image = NSImage(size: NSSize(width: 18, height: 18), flipped: false) { _ in
            draw()
            return true
        }
        image.isTemplate = false
        image.accessibilityDescription = "Robin"
        return image
    }

    static func draw() {
        guard let ctx = NSGraphicsContext.current else { return }
        let ink = NSColor.black
        let centre = NSPoint(x: 9, y: 9)

        ink.setFill()
        for i in 0..<5 {
            // A petal pointing up, turned into place around the centre.
            let petal = NSBezierPath(ovalIn: NSRect(x: -2.7, y: 1.6, width: 5.4, height: 7.4))
            let turn = AffineTransform(rotationByDegrees: CGFloat(i) * 72)
            petal.transform(using: turn)
            petal.transform(using: AffineTransform(translationByX: centre.x, byY: centre.y))
            petal.fill()
        }

        ctx.saveGraphicsState()
        ctx.compositingOperation = .clear
        NSBezierPath(ovalIn: NSRect(x: centre.x - 2.6, y: centre.y - 2.6, width: 5.2, height: 5.2)).fill()
        // Thin cuts between the petals, so they read as five at 18 pt.
        let cuts = NSBezierPath()
        for i in 0..<5 {
            let a = (CGFloat(i) * 72 + 36 + 90) * .pi / 180
            cuts.move(to: NSPoint(x: centre.x + 2 * cos(a), y: centre.y + 2 * sin(a)))
            cuts.line(to: NSPoint(x: centre.x + 9 * cos(a), y: centre.y + 9 * sin(a)))
        }
        cuts.lineWidth = 0.8
        cuts.stroke()
        ctx.restoreGraphicsState()

        NSBezierPath(ovalIn: NSRect(x: centre.x - 1.5, y: centre.y - 1.5, width: 3, height: 3)).fill()

        // Paint the shape periwinkle at the top to purple at the bottom.
        ctx.saveGraphicsState()
        ctx.compositingOperation = .sourceAtop
        let periwinkle = NSColor(srgbRed: 0.62, green: 0.66, blue: 1.0, alpha: 1)
        let purple = NSColor(srgbRed: 0.66, green: 0.42, blue: 0.98, alpha: 1)
        NSGradient(starting: purple, ending: periwinkle)?.draw(in: NSRect(x: 0, y: 0, width: 18, height: 18), angle: 90)
        ctx.restoreGraphicsState()
    }
}
