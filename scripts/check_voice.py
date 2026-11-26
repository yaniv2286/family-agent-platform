import argparse
import json
from pathlib import Path
from playwright.sync_api import sync_playwright


MOCKS = r"""
window.voiceMock = {recognitions: [], recorders: [], stopped: 0, resumed: 0};
class Recognition {
    constructor() { voiceMock.recognitions.push(this); }
    start() { this.starts = (this.starts || 0) + 1; queueMicrotask(() => this.onstart?.()); }
    stop() {}
}
window.SpeechRecognition = Recognition;
window.webkitSpeechRecognition = Recognition;
Object.defineProperty(navigator, 'mediaDevices', {value: {
    getUserMedia: async () => ({getTracks: () => [{stop: () => voiceMock.stopped++}]})
}, configurable: true});
window.AudioContext = class {
    constructor() { this.state = 'suspended'; }
    async resume() { voiceMock.resumed++; this.state = 'running'; }
    async close() {}
    createAnalyser() { return {fftSize: 1024, getByteTimeDomainData: data => data.fill(voiceMock.level ?? 128)}; }
    createMediaStreamSource() { return {connect() {}}; }
};
window.MediaRecorder = class {
    static isTypeSupported() { return true; }
    constructor(stream, options) { this.mimeType = options?.mimeType || 'audio/webm'; this.state = 'inactive'; voiceMock.recorders.push(this); }
    start() { this.state = 'recording'; }
    stop() {
        this.state = 'inactive';
        queueMicrotask(() => {
            this.ondataavailable?.({data: new Blob(['mock audio'])});
            this.onstop?.();
        });
    }
};
"""


def run():
    parser = argparse.ArgumentParser()
    parser.add_argument('--browser', default='chromium', choices=['chromium', 'webkit'])
    args = parser.parse_args()
    html = Path('static/index.html').read_text(encoding='utf-8')
    html = html.replace('        // State', '        window.voiceTest = {startListening, stopListening, finishListening, isListening: () => isListening, speak: t => speakResponse(t), setup: () => {selectedUserId = 1; selectedSubject = "math";}};\n        // State', 1)
    failures = []
    with sync_playwright() as p:
        browser = getattr(p, args.browser).launch(**({'channel': 'chrome'} if args.browser == 'chromium' else {}), headless=True)
        for mobile, agent in [(False, 'Desktop Chrome'), (True, 'Mozilla/5.0 (Linux; Android 14) Chrome/130.0 Mobile Safari/537.36'),
                              (True, 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)')]:
            page = browser.new_page(viewport={'width': 390, 'height': 844}, is_mobile=mobile, has_touch=mobile,
                                    user_agent=agent)
            page.add_init_script(MOCKS)
            chats, uploads, errors = [], [], []
            page.on('pageerror', lambda error: errors.append(str(error)))

            def api(route):
                if route.request.url.endswith('/chat'):
                    chats.append(route.request.post_data_json)
                    route.fulfill(json={'reply': 'כל הכבוד', 'points_earned': 1})
                elif route.request.url.endswith('/transcribe'):
                    uploads.append(route.request.post_data)
                    route.fulfill(json={'text': 'ארבעים ושתיים'})
                elif route.request.url.endswith('/speech'):
                    route.fulfill(status=204)
                else:
                    route.fulfill(json={})

            page.route('**/api/**', api)
            page.route('http://voice.test/', lambda route: route.fulfill(body=html, content_type='text/html'))
            page.goto('http://voice.test/')
            page.evaluate('voiceTest.setup()')
            page.evaluate('voiceTest.startListening()')
            page.wait_for_timeout(50)
            if mobile:
                try:
                    assert page.evaluate('voiceMock.recorders.length') == 1, 'mobile did not start recorder'
                    assert page.evaluate('voiceMock.resumed') == 1, 'audio context stayed suspended'
                    page.evaluate('voiceTest.finishListening(true)')
                    page.wait_for_timeout(300)
                    assert len(uploads) == 1, 'manual stop did not upload'
                    page.evaluate('voiceTest.startListening()')
                    page.evaluate('voiceTest.stopListening()')
                    page.wait_for_timeout(100)
                    assert len(uploads) == 1, 'cancel uploaded audio'
                    page.evaluate('''() => {
                        navigator.mediaDevices.getUserMedia = () => new Promise(resolve => { voiceMock.grant = resolve; });
                        void voiceTest.startListening();
                        voiceTest.stopListening();
                        voiceMock.grant({getTracks: () => [{stop: () => voiceMock.stopped++}]});
                    }''')
                    page.wait_for_timeout(100)
                    assert page.evaluate('voiceMock.recorders.length') == 2, 'late permission restarted recording'
                    assert page.evaluate('voiceMock.stopped') == 3, 'late microphone stream leaked'
                    page.evaluate('''() => {
                        navigator.mediaDevices.getUserMedia = async () => ({getTracks: () => [{stop() {}}]});
                        AudioContext.prototype.resume = () => new Promise(() => {});
                    }''')
                    page.evaluate('voiceTest.startListening()')
                    assert page.evaluate('voiceMock.recorders.length') == 3, 'suspended audio analyser blocked recording'
                    page.evaluate('voiceTest.stopListening()')
                    print('PASS mobile recorder, audio resume, manual submit, cancellation, late permission cleanup, suspended analyser')
                except AssertionError as error:
                    failures.append(str(error))
            else:
                page.evaluate('''() => {
                    const r = voiceMock.recognitions[0];
                    const result = [{transcript: 'ארבעים ושתיים'}];
                    result.isFinal = true;
                    r.onresult({results: [result]});
                    r.onend();
                }''')
                page.wait_for_timeout(3100)
                try:
                    assert len(chats) == 1, 'recognition end lost pending transcript'
                    print('PASS desktop recognition end preserves transcript')
                except AssertionError as error:
                    failures.append(str(error))
            assert not errors, errors
            page.close()
        check_push_to_talk(browser, html, failures)
        check_tts_stream_retry(browser, html, failures)
        browser.close()
    if failures:
        raise AssertionError(json.dumps(failures))


def check_push_to_talk(browser, html, failures):
    """Hold Space = listen, release = send. Also long-press on the mic."""
    page = browser.new_page(viewport={'width': 1280, 'height': 800})
    page.add_init_script(MOCKS)
    uploads, errors, ended = [], [], []
    page.on('pageerror', lambda error: errors.append(str(error)))

    def api(route):
        url = route.request.url
        if url.endswith('/transcribe'):
            uploads.append(1)
            route.fulfill(json={'text': 'my name is Noya'})
        elif url.endswith('/chat'):
            route.fulfill(json={'reply': 'Nice to meet you', 'points_earned': 1})
        elif url.endswith('/end-session'):
            ended.append(1)
            route.fulfill(json={})
        elif url.endswith('/speech'):
            route.fulfill(status=204)
        else:
            route.fulfill(json={})

    page.route('**/api/**', api)
    page.route('http://voice.test/', lambda route: route.fulfill(body=html, content_type='text/html'))
    page.goto('http://voice.test/')
    page.evaluate("""() => {
        voiceTest.setup();
        document.querySelector('#pin-modal').classList.remove('show');
        document.querySelector('#app-container').style.display = 'block';
        document.querySelector('#tutoring-screen').classList.add('show');
        document.activeElement?.blur();
    }""")
    recorders = lambda: page.evaluate('voiceMock.recorders.length')
    listening = lambda: page.evaluate('voiceTest.isListening()')
    try:
        # 1. Long hold with a pause in the middle: silence must NOT auto-send.
        page.keyboard.down('Space')
        page.wait_for_timeout(1150)
        page.evaluate('voiceMock.level = 200')   # child speaks
        page.keyboard.down('Space')              # auto-repeat while held
        page.wait_for_timeout(400)
        page.evaluate('voiceMock.level = 128')   # child pauses to think
        page.wait_for_timeout(3000)
        assert recorders() == 1, f'key repeat started {recorders()} recorders'
        assert uploads == [], 'silence auto-submitted while Space was held'
        page.keyboard.up('Space')
        page.wait_for_timeout(300)
        assert len(uploads) == 1, 'release did not send the recording'
        assert page.evaluate('window.scrollY') == 0, 'Space scrolled the page'

        # 2. Accidental tap: cancelled, nothing sent.
        page.keyboard.down('Space')
        page.wait_for_timeout(120)
        page.keyboard.up('Space')
        page.wait_for_timeout(300)
        assert len(uploads) == 1, 'a short tap was sent'

        # 3. Typing a space in the text box never opens the mic.
        before = recorders()
        page.locator('#text-input').focus()
        page.keyboard.press('Space')
        assert recorders() == before, 'Space in text input opened the mic'
        assert page.locator('#text-input').input_value() == ' ', 'Space was swallowed in the text input'
        page.locator('#text-input').fill('')

        # 4. Space on a focused button triggers PTT, not the button.
        page.locator('#end-session-button').focus()
        page.keyboard.down('Space')
        page.wait_for_timeout(500)
        page.keyboard.up('Space')
        page.wait_for_timeout(300)
        assert ended == [], 'Space activated the focused end-session button'
        assert not page.evaluate("document.querySelector('#session-modal').classList.contains('show')"), 'Space opened the end-session modal'
        assert len(uploads) == 2, 'PTT from a focused button did not send'
        page.evaluate('document.activeElement.blur()')

        # 5. A modal (games) is open: Space does nothing.
        before = recorders()
        page.evaluate('document.body.appendChild(Object.assign(document.createElement("div"), {className: "vm-modal-overlay"}))')
        page.keyboard.down('Space')
        page.wait_for_timeout(200)
        page.keyboard.up('Space')
        assert recorders() == before, 'Space opened the mic behind a modal'
        page.evaluate('document.querySelector(".vm-modal-overlay").remove()')

        # 6. Window loses focus mid-hold: treated as release, mic closed.
        page.keyboard.down('Space')
        page.wait_for_timeout(500)
        page.evaluate("window.dispatchEvent(new Event('blur'))")
        page.wait_for_timeout(300)
        assert len(uploads) == 3, 'blur mid-hold did not send'
        assert not listening(), 'mic left open after blur'
        page.keyboard.up('Space')
        page.wait_for_timeout(200)
        assert len(uploads) == 3, 'keyup after blur sent twice'

        # 7. Long-press the mic button: same behaviour, no extra toggle.
        before = recorders()
        page.locator('#voice-button').dispatch_event('pointerdown', {'button': 0, 'pointerType': 'touch'})
        page.wait_for_timeout(900)
        page.locator('#voice-button').dispatch_event('pointerup', {'button': 0, 'pointerType': 'touch'})
        page.locator('#voice-button').dispatch_event('click')
        page.wait_for_timeout(300)
        assert recorders() == before + 1, 'long-press did not record exactly once'
        assert len(uploads) == 4, 'long-press release did not send'
        assert not listening(), 'click after long-press reopened the mic'

        # 8. A short tap on the mic still uses the old toggle.
        # (dispatch the click directly: the listening button pulses, so
        # Playwright's real-mouse click never sees a "stable" element.)
        page.locator('#voice-button').dispatch_event('click')
        page.wait_for_timeout(100)
        assert listening(), 'tap no longer starts listening'
        page.locator('#voice-button').dispatch_event('click')
        page.wait_for_timeout(300)
        assert len(uploads) == 5, 'second tap no longer sends'
        assert not errors, errors
        print('PASS push-to-talk: hold+pause, key repeat, short tap cancel, text input, focused button, modal, blur, long-press, tap toggle')
    except AssertionError as error:
        failures.append(f'push-to-talk: {error}')
    page.close()


def check_tts_stream_retry(browser, html, failures):
    """A mid-stream MediaSource failure must refetch the same utterance as a
    whole blob (ינאי's 'אני לא שומע אותך': a dying stream left Koko silent)."""
    page = browser.new_page(viewport={'width': 390, 'height': 844})
    page.add_init_script(MOCKS)
    page.add_init_script(r"""
        // Every SourceBuffer append fails the way a mid-play network error
        // does - this is the real failure mode behind the silent tutor.
        MediaSource.prototype.addSourceBuffer = function () {
            const fake = new EventTarget();
            fake.buffered = {length: 0, end() { return 0; }};
            fake.appendBuffer = () => queueMicrotask(() => fake.dispatchEvent(new Event('error')));
            return fake;
        };
    """)
    speech_calls, logs, errors = [], [], []
    page.on('pageerror', lambda e: errors.append(str(e)))

    def api(route):
        url = route.request.url
        if url.endswith('/speech'):
            speech_calls.append(route.request.post_data_json)
            route.fulfill(status=200, content_type='audio/mpeg', body=b'\xff\xfb\x90\x64' * 64)
        elif url.endswith('/client-log'):
            logs.append(route.request.post_data_json)
            route.fulfill(json={})
        else:
            route.fulfill(json={})

    page.route('**/api/**', api)
    page.route('http://voice.test/', lambda r: r.fulfill(body=html, content_type='text/html'))
    page.goto('http://voice.test/')
    page.evaluate('voiceTest.setup()')
    page.evaluate('voiceTest.speak("כל הכבוד")')
    page.wait_for_timeout(2500)
    try:
        assert len(speech_calls) == 2, f'mid-stream error did not retry (got {len(speech_calls)} speech calls)'
        assert speech_calls[0].get('text') == speech_calls[1].get('text'), 'retry used different text'
        details = [l.get('detail', '') for l in logs]
        assert any('mid-stream retry' in d for d in details), f'no retry client-log: {details}'
        assert not errors, errors
        print('PASS tts mid-stream error retries as full blob')
    except AssertionError as e:
        failures.append(f'tts-retry: {e}')
    page.close()


if __name__ == '__main__':
    run()
