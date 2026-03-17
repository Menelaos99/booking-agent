# --- Login page ---
LOGIN_EMAIL_INPUT = 'input[name="loginname"], input[name="username"], input[type="email"]'
LOGIN_NEXT_BUTTON = 'button[type="submit"], button:has-text("Next"), button:has-text("Continue")'
LOGIN_PASSWORD_INPUT = 'input[name="password"], input[type="password"]'
LOGIN_SUBMIT_BUTTON = 'button[type="submit"], button:has-text("Sign in"), button:has-text("Log in"), button:has-text("Submit"), [data-testid="login-button"]'

# OTP input
OTP_INPUT = 'input[name="otp"], input[name="code"]'
OTP_SUBMIT_BUTTON = 'button[type="submit"], button:has-text("Verify"), button:has-text("Continue")'

# 2FA / CAPTCHA detection
TWO_FA_INDICATOR = 'input[name="otp"], input[name="code"], [data-testid="two-factor"]'
CAPTCHA_INDICATOR = 'iframe[src*="captcha"], iframe[src*="recaptcha"], [class*="captcha"]'

# Post-login verification
EXTRANET_INDICATOR = '[data-testid="extranet"], .bui-navbar, .extranet-navigation, #main-nav'

# --- Reservations ---
RESERVATIONS_TABLE = 'table.reservations, [data-testid="reservations-table"], .bui-table'
RESERVATION_ROW = "tbody tr"
RESERVATION_GUEST_NAME = "td:nth-child(2)"
RESERVATION_CHECK_IN = "td:nth-child(3)"
RESERVATION_CHECK_OUT = "td:nth-child(4)"
RESERVATION_STATUS = "td:nth-child(5)"
RESERVATION_TOTAL = "td:nth-child(6)"
RESERVATION_ID_LINK = "td:nth-child(1) a"

# --- Messages ---
MESSAGES_LIST = '.messages-list, [data-testid="messages-list"]'
MESSAGE_ITEM = ".message-item, .conversation-item"
MESSAGE_UNREAD = '.unread, [data-testid="unread"]'
MESSAGE_GUEST_NAME = ".guest-name, .sender-name"
MESSAGE_SUBJECT = ".message-subject, .subject"
MESSAGE_DATE = ".message-date, .date"
MESSAGE_BODY = ".message-body, .message-content"
MESSAGE_REPLY_INPUT = 'textarea[name="reply"], textarea.reply-input'
MESSAGE_SEND_BUTTON = 'button:has-text("Send"), button[type="submit"]'

# --- Pricing ---
PRICING_CALENDAR = '.rate-calendar, [data-testid="pricing-calendar"]'
PRICING_CELL = ".calendar-cell, .rate-cell"
PRICING_INPUT = 'input[name="price"], input.rate-input'
PRICING_SAVE = 'button:has-text("Save"), button[type="submit"]'

# --- Availability ---
AVAILABILITY_CALENDAR = '.availability-calendar, [data-testid="availability-calendar"]'
AVAILABILITY_CELL = ".calendar-cell, .availability-cell"
AVAILABILITY_OPEN_BUTTON = 'button:has-text("Open"), button:has-text("Available")'
AVAILABILITY_CLOSE_BUTTON = 'button:has-text("Close"), button:has-text("Unavailable")'

# --- Performance / Stats ---
STATS_CONTAINER = '.performance-dashboard, [data-testid="performance"]'
STATS_SCORE = ".property-score, .review-score"
STATS_VIEWS = ".page-views, .views-count"
STATS_BOOKINGS_COUNT = ".bookings-count, .conversion"
