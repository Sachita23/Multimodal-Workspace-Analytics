// The Focus Assistant's deviation popup, drawn natively on macOS.
//
// Same look as focus_gui.DeviationPopup (dark box, red border, bottom-right
// corner), but as a non-activating floating panel that shows over every app,
// every Space and full-screen apps, and never takes focus.
//
// Protocol (same as `python -m focus.overlay`), one JSON object per line:
//   stdin:  {"cmd":"show","app","window_title","goal","allowed_apps":[...]}
//           {"cmd":"hide"} | {"cmd":"quit"}            (EOF quits too)
//   stdout: {"action":"allow","app":"..."} | {"action":"snooze"}
//
// Built on first use by focus/overlay.py (native_helper):
//   clang -fobjc-arc -O2 -framework Cocoa -o focus_popup macos_popup.m

#import <Cocoa/Cocoa.h>

static const CGFloat kWidth = 390, kHeight = 268, kMarginRight = 28, kMarginBottom = 72;

static NSColor *Hex(unsigned rgb) {
    return [NSColor colorWithSRGBRed:((rgb >> 16) & 0xFF) / 255.0
                               green:((rgb >> 8) & 0xFF) / 255.0
                                blue:(rgb & 0xFF) / 255.0
                               alpha:1.0];
}

static NSTextField *Label(NSString *text, NSFont *font, NSColor *color) {
    NSTextField *label = [NSTextField wrappingLabelWithString:text];
    label.font = font;
    label.textColor = color;
    label.drawsBackground = NO;
    label.bezeled = NO;
    label.editable = NO;
    label.selectable = NO;
    label.preferredMaxLayoutWidth = kWidth - 52;
    return label;
}

@interface PopupPanel : NSPanel
@end

@implementation PopupPanel
// Buttons work, but the panel never becomes the key or main window, so the
// app the user is in keeps keyboard focus.
- (BOOL)canBecomeKeyWindow { return NO; }
- (BOOL)canBecomeMainWindow { return NO; }
@end

@interface Popup : NSObject
@property(strong) PopupPanel *panel;
@property(strong) NSTextField *appLabel, *messageLabel, *goalLabel;
@property(copy) NSString *application;
@end

@implementation Popup

- (NSButton *)button:(NSString *)title background:(NSColor *)background action:(SEL)action {
    NSButton *button = [NSButton buttonWithTitle:title target:self action:action];
    button.bordered = NO;
    button.wantsLayer = YES;
    button.layer.backgroundColor = background.CGColor;
    button.layer.cornerRadius = 6;
    button.attributedTitle = [[NSAttributedString alloc]
        initWithString:title
            attributes:@{NSForegroundColorAttributeName: Hex(0xFFFFFF),
                         NSFontAttributeName: [NSFont boldSystemFontOfSize:12]}];
    [button.heightAnchor constraintEqualToConstant:30].active = YES;
    return button;
}

- (instancetype)init {
    if (!(self = [super init])) return nil;

    self.panel = [[PopupPanel alloc]
        initWithContentRect:NSMakeRect(0, 0, kWidth, kHeight)
                  styleMask:NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
                    backing:NSBackingStoreBuffered
                      defer:NO];
    NSPanel *panel = self.panel;
    panel.floatingPanel = YES;
    panel.hidesOnDeactivate = NO;
    panel.becomesKeyOnlyIfNeeded = YES;
    panel.level = NSStatusWindowLevel;
    panel.collectionBehavior = NSWindowCollectionBehaviorCanJoinAllSpaces |
                               NSWindowCollectionBehaviorFullScreenAuxiliary |
                               NSWindowCollectionBehaviorStationary |
                               NSWindowCollectionBehaviorIgnoresCycle;
    panel.opaque = NO;
    panel.hasShadow = YES;
    panel.backgroundColor = NSColor.clearColor;

    // Red border (4 px) around the dark card, like the Tk popup.
    NSView *border = [[NSView alloc] initWithFrame:NSMakeRect(0, 0, kWidth, kHeight)];
    border.wantsLayer = YES;
    border.layer.backgroundColor = Hex(0xFF6377).CGColor;
    border.layer.cornerRadius = 4;
    panel.contentView = border;

    NSView *card = [[NSView alloc] init];
    card.wantsLayer = YES;
    card.layer.backgroundColor = Hex(0x11151C).CGColor;
    card.translatesAutoresizingMaskIntoConstraints = NO;
    [border addSubview:card];
    [NSLayoutConstraint activateConstraints:@[
        [card.leadingAnchor constraintEqualToAnchor:border.leadingAnchor constant:4],
        [card.trailingAnchor constraintEqualToAnchor:border.trailingAnchor constant:-4],
        [card.topAnchor constraintEqualToAnchor:border.topAnchor constant:4],
        [card.bottomAnchor constraintEqualToAnchor:border.bottomAnchor constant:-4],
    ]];

    NSTextField *heading = Label(@"●  FOCUS DEVIATION", [NSFont boldSystemFontOfSize:11], Hex(0xFF6377));
    self.appLabel = Label(@"", [NSFont boldSystemFontOfSize:18], Hex(0xF4F7FB));
    self.messageLabel = Label(@"", [NSFont systemFontOfSize:12], Hex(0x9BAABB));
    self.goalLabel = Label(@"", [NSFont systemFontOfSize:11], Hex(0x65768A));
    NSTextField *reminder = Label(@"This reminder stays until you return to your focus zone.",
                                  [NSFont boldSystemFontOfSize:11], Hex(0x64A4FF));

    NSButton *allow = [self button:@"Allow for this session" background:Hex(0x3F86FF) action:@selector(allow:)];
    NSButton *snooze = [self button:@"Snooze 5 min" background:Hex(0x151F2B) action:@selector(snooze:)];
    [allow.widthAnchor constraintEqualToConstant:170].active = YES;
    [snooze.widthAnchor constraintEqualToConstant:120].active = YES;
    NSStackView *buttons = [NSStackView stackViewWithViews:@[allow, snooze]];
    buttons.spacing = 8;

    NSStackView *stack = [NSStackView stackViewWithViews:@[heading, self.appLabel, self.messageLabel,
                                                           self.goalLabel, reminder, buttons]];
    stack.orientation = NSUserInterfaceLayoutOrientationVertical;
    stack.alignment = NSLayoutAttributeLeading;
    stack.spacing = 8;
    [stack setCustomSpacing:12 afterView:heading];
    [stack setCustomSpacing:12 afterView:self.goalLabel];
    stack.translatesAutoresizingMaskIntoConstraints = NO;
    [card addSubview:stack];
    [NSLayoutConstraint activateConstraints:@[
        [stack.leadingAnchor constraintEqualToAnchor:card.leadingAnchor constant:20],
        [stack.trailingAnchor constraintLessThanOrEqualToAnchor:card.trailingAnchor constant:-20],
        [stack.topAnchor constraintEqualToAnchor:card.topAnchor constant:16],
    ]];
    return self;
}

- (void)emit:(NSDictionary *)message {
    NSData *data = [NSJSONSerialization dataWithJSONObject:message options:0 error:nil];
    if (!data) return;
    fwrite(data.bytes, 1, data.length, stdout);
    fputc('\n', stdout);
    fflush(stdout);
}

- (void)allow:(id)sender {
    [self emit:@{@"action": @"allow", @"app": self.application ?: @""}];
}

- (void)snooze:(id)sender {
    [self emit:@{@"action": @"snooze"}];
}

- (void)place {
    NSScreen *screen = NSScreen.mainScreen ?: NSScreen.screens.firstObject;
    NSRect visible = screen.visibleFrame;
    [self.panel setFrameOrigin:NSMakePoint(NSMaxX(visible) - kWidth - kMarginRight,
                                           NSMinY(visible) + kMarginBottom)];
}

- (void)show:(NSDictionary *)message {
    NSString *(^text)(NSString *) = ^NSString *(NSString *key) {
        id value = message[key];
        return [value isKindOfClass:NSString.class] ? value : @"";
    };

    self.application = text(@"app");
    self.appLabel.stringValue = self.application.length ? self.application : @"Unknown application";

    NSString *detail = @"Switch back to one of your selected applications.";
    NSString *title = text(@"window_title");
    if (title.length && ![title isEqualToString:@"Unknown"]) {
        detail = [NSString stringWithFormat:@"Current window: %@\n\n%@", title, detail];
    }
    self.messageLabel.stringValue = detail;

    NSArray *apps = [message[@"allowed_apps"] isKindOfClass:NSArray.class] ? message[@"allowed_apps"] : @[];
    self.goalLabel.stringValue = [NSString stringWithFormat:@"Goal: %@\nFocus zone: %@",
                                  text(@"goal"), [apps componentsJoinedByString:@", "]];

    [self place];
    self.panel.alphaValue = 1.0;
    self.panel.ignoresMouseEvents = NO;
    [self.panel orderFrontRegardless];
}

- (void)hide {
    // Never ordered out: an all-Spaces status-level panel can get stuck
    // after orderOut: (still listed on screen, then not shown again). It
    // stays in place and turns invisible and click-through instead.
    self.panel.alphaValue = 0.0;
    self.panel.ignoresMouseEvents = YES;
}

- (void)handle:(NSString *)line {
    NSData *data = [line dataUsingEncoding:NSUTF8StringEncoding];
    id message = data ? [NSJSONSerialization JSONObjectWithData:data options:0 error:nil] : nil;
    if (![message isKindOfClass:NSDictionary.class]) return;

    NSString *cmd = message[@"cmd"];
    if ([cmd isEqualToString:@"show"]) [self show:message];
    else if ([cmd isEqualToString:@"hide"]) [self hide];
    else if ([cmd isEqualToString:@"quit"]) [NSApp terminate:nil];
}

@end

int main(void) {
    @autoreleasepool {
        NSApplication *app = NSApplication.sharedApplication;
        // No Dock icon, no menu bar, never the active app.
        app.activationPolicy = NSApplicationActivationPolicyAccessory;

        Popup *popup = [[Popup alloc] init];

        [NSThread detachNewThreadWithBlock:^{
            char buffer[65536];
            while (fgets(buffer, sizeof buffer, stdin)) {
                NSString *line = [NSString stringWithUTF8String:buffer];
                if (!line) continue;
                dispatch_async(dispatch_get_main_queue(), ^{ [popup handle:line]; });
            }
            dispatch_async(dispatch_get_main_queue(), ^{ [NSApp terminate:nil]; });
        }];

        [app run];
    }
    return 0;
}
