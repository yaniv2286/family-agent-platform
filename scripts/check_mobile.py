import argparse
from pathlib import Path
from playwright.sync_api import sync_playwright


def run():
    parser = argparse.ArgumentParser()
    parser.add_argument('--browser', default='chromium', choices=['chromium', 'webkit'])
    args = parser.parse_args()
    html = Path('static/index.html').read_text(encoding='utf-8')
    html = html.replace('        // State', '        window.mobileTest = {openVisualSolver, openGamesMenu, addMessage, setSubject: s => selectedSubject = s, setProfile: p => selectedUserProfile = p};\n        // State', 1)
    with sync_playwright() as p:
        engine = getattr(p, args.browser)
        browser = engine.launch(**({'channel': 'chrome'} if args.browser == 'chromium' else {}), headless=True)
        # Phones (incl. the smallest common one) plus an iPad-sized tablet.
        for width, height in [(320, 640), (390, 844), (430, 932), (844, 390), (768, 1024)]:
            page = browser.new_page(viewport={'width': width, 'height': height}, is_mobile=True, has_touch=True)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/api/**', lambda route: route.fulfill(json={}))
            page.route('http://mobile.test/', lambda route: route.fulfill(body=html, content_type='text/html'))
            page.goto('http://mobile.test/')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'PIN overflow'
            for screen in ['profile-screen', 'subject-screen', 'fitness-screen']:
                page.evaluate('''id => {
                    document.querySelector('#pin-modal').style.display = 'none';
                    document.querySelector('#app-container').style.display = 'block';
                    document.querySelectorAll('.screen').forEach(e => e.classList.add('hidden'));
                    document.querySelectorAll('.fitness-screen').forEach(e => e.classList.remove('show'));
                    const screen = document.getElementById(id);
                    screen.classList.remove('hidden');
                    screen.classList.add('show');
                }''', screen)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width, screen)
            page.evaluate('''() => {
                document.querySelector('.fitness-screen').classList.remove('show');
                document.querySelector('#pin-modal').style.display = 'none';
                document.querySelector('#app-container').style.display = 'block';
                document.querySelectorAll('.screen').forEach(e => e.classList.add('hidden'));
                document.querySelector('#tutoring-screen').classList.add('show');
            }''')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width, 'page overflow')
            if height > width:
                assert page.locator('.input-area').bounding_box()['y'] + page.locator('.input-area').bounding_box()['height'] <= height + 1
                assert page.locator('.chat-container').bounding_box()['height'] >= 150
            for text in ['כמה זה 825 פחות 462?', 'What do you like to eat for lunch? ' * 8]:
                page.evaluate('text => mobileTest.addMessage("assistant", text)', text)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.locator('#text-input').fill('Hello Koko')
            page.locator('#text-input').clear()
            page.evaluate('mobileTest.openVisualSolver(825, "-", 462)')
            assert page.evaluate('document.querySelector(".vm-modal").scrollWidth <= document.querySelector(".vm-modal").clientWidth')
            page.locator('.vm-numpad button').filter(has_text='3').first.tap()
            page.locator('.vm-close').tap()
            page.locator('#games-button').tap()
            names = page.locator('.vm-modal .vm-key').all_text_contents()
            # No grade in profile -> upper-elementary tier -> 5 math games
            # (the fraction/decimal game is tier-gated to grades 4-5).
            assert len(names) == 5, names
            for index in range(len(names)):
                page.locator('.vm-modal .vm-key').nth(index).tap()
                assert page.locator('.vm-modal').is_visible()
                assert page.evaluate('document.querySelector(".vm-modal").scrollWidth <= document.querySelector(".vm-modal").clientWidth'), names[index]
                page.locator('.vm-close').tap()
                if index < len(names) - 1:
                    page.locator('#games-button').tap()
            # 1st-grade profile: fraction game must be hidden.
            page.evaluate('mobileTest.setProfile({"name": "test", "grade_level": "1st Grade"})')
            page.locator('#games-button').tap()
            names = page.locator('.vm-modal .vm-key').all_text_contents()
            assert len(names) == 4, names
            page.locator('.vm-close').tap()
            page.evaluate('mobileTest.setProfile(null)')
            # English subject: same button must open English games, not math.
            page.evaluate('mobileTest.setSubject("english")')
            page.locator('#games-button').tap()
            names = page.locator('.vm-modal .vm-key').all_text_contents()
            assert len(names) == 5, names
            assert any('משפט' in n or 'שמעת' in n for n in names), names
            for index in range(5):
                page.locator('.vm-modal .vm-key').nth(index).tap()
                assert page.locator('.vm-modal').is_visible()
                assert page.evaluate('document.querySelector(".vm-modal").scrollWidth <= document.querySelector(".vm-modal").clientWidth'), names[index]
                page.locator('.vm-close').tap()
                if index < 4:
                    page.locator('#games-button').tap()
            # Kindergarten profile: reading games must be hidden.
            page.evaluate('mobileTest.setProfile({"name": "ליבי", "grade_level": "Kindergarten"})')
            page.locator('#games-button').tap()
            names = page.locator('.vm-modal .vm-key').all_text_contents()
            assert len(names) == 3, names  # emoji + syllables + listen only
            page.locator('.vm-close').tap()
            page.evaluate('mobileTest.setSubject("math")')
            page.locator('#games-button').tap()
            names = page.locator('.vm-modal .vm-key').all_text_contents()
            assert len(names) == 3, names  # no Kakooma, no fractions
            page.locator('.vm-close').tap()
            page.evaluate('mobileTest.setProfile(null)')
            assert not errors, errors
            print(f'PASS {args.browser} {width}x{height}: chat, input, solver touch, math+english game screens, no horizontal overflow or JS errors')
            page.close()
        browser.close()


if __name__ == '__main__':
    run()
