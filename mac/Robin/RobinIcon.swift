//
//  RobinIcon.swift
//  Robin
//
//  The menu bar robin: a template image, so macOS tints it for light and dark menu bars.
//

import AppKit

enum RobinIcon {
    /// An 18×18 pt robin facing right; the breast is lighter, the eye and wing line are cut out.
    static func bird() -> NSImage {
        let image = NSImage(size: NSSize(width: 18, height: 18), flipped: false) { _ in
            draw()
            return true
        }
        image.isTemplate = true
        image.accessibilityDescription = "Robin"
        return image
    }

    static func draw() {
        guard let ctx = NSGraphicsContext.current else { return }
        let ink = NSColor.black

        let body = NSBezierPath(ovalIn: NSRect(x: 3, y: 3.6, width: 11.2, height: 8.6))
        let head = NSBezierPath(ovalIn: NSRect(x: 9.8, y: 8.4, width: 6.4, height: 6.4))
        let tail = NSBezierPath()
        tail.move(to: NSPoint(x: 5.2, y: 10.6))
        tail.line(to: NSPoint(x: 0.6, y: 13.4))
        tail.line(to: NSPoint(x: 0.4, y: 11.6))
        tail.line(to: NSPoint(x: 3.6, y: 7.4))
        tail.close()
        let beak = NSBezierPath()
        beak.move(to: NSPoint(x: 15.6, y: 12.4))
        beak.line(to: NSPoint(x: 17.9, y: 11.5))
        beak.line(to: NSPoint(x: 15.6, y: 10.6))
        beak.close()

        ink.setFill()
        for part in [body, head, tail, beak] { part.fill() }

        let legs = NSBezierPath()
        legs.move(to: NSPoint(x: 7.8, y: 4.2))
        legs.line(to: NSPoint(x: 7.2, y: 1.2))
        legs.move(to: NSPoint(x: 10.2, y: 4.2))
        legs.line(to: NSPoint(x: 10.8, y: 1.2))
        legs.lineWidth = 1.1
        legs.lineCapStyle = .round
        ink.setStroke()
        legs.stroke()

        ctx.saveGraphicsState()
        ctx.compositingOperation = .copy

        // The breast, lighter than the rest of the bird.
        let breast = NSBezierPath(ovalIn: NSRect(x: 10.2, y: 2.6, width: 8, height: 8.6))
        let silhouette = NSBezierPath()
        silhouette.append(body)
        silhouette.append(head)
        silhouette.addClip()
        ink.withAlphaComponent(0.45).setFill()
        breast.fill()
        ctx.restoreGraphicsState()

        ctx.saveGraphicsState()
        ctx.compositingOperation = .clear
        NSBezierPath(ovalIn: NSRect(x: 13.2, y: 11.6, width: 1.3, height: 1.3)).fill()
        let wing = NSBezierPath()
        wing.move(to: NSPoint(x: 4.4, y: 9.4))
        wing.curve(to: NSPoint(x: 9.2, y: 6.4), controlPoint1: NSPoint(x: 6.4, y: 10.0), controlPoint2: NSPoint(x: 8.2, y: 8.6))
        wing.lineWidth = 0.9
        wing.lineCapStyle = .round
        wing.stroke()
        ctx.restoreGraphicsState()
    }
}
