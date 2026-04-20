from flask import Flask, request, jsonify
import json, re, os
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

# ── Google Sheets ─────────────────────────────────────────────────────────────
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']

HEADERS = [
    'timestamp','session_id','mood',
    'total_chars','word_count','avg_word_length','sentence_count',
    'typing_duration_sec','avg_typing_speed_wpm','first_keystroke_delay_ms',
    'avg_key_hold_time_ms','hold_time_variance',
    'avg_inter_key_delay_ms','delay_variance',
    'rhythm_irregularity','avg_digraph_latency_ms',
    'pause_count','longest_pause_ms','avg_pause_duration_ms',
    'burst_count','avg_burst_length',
    'backspace_count','backspace_ratio',
    'correction_burst_count','max_correction_burst',
    'caps_ratio','exclamation_count','question_count',
    'repeated_letters_count','profanity_count','hedging_word_count',
    'avg_sentence_length','error_rate',
    'sentiment_score','dominant_emotion',
    'text_sample'
]

PROFANITY = {'damn','hell','crap','stupid','idiot','hate','worst','ugh','wtf','dumb','moron'}
HEDGING   = ['maybe','perhaps','might','possibly','probably','i think','i guess',
             'i suppose','sort of','kind of','not sure','i feel like','seems like','apparently']
COMMON_DG = {'th','he','in','er','an','re','on','en','at','es','st','nt','ou','ea','hi','is','it','ha','et'}

def get_sheet():
    raw   = os.environ.get('GOOGLE_CREDENTIALS','{}')
    sid   = os.environ.get('SHEET_ID','')
    creds = Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES)
    gc    = gspread.authorize(creds)
    sh    = gc.open_by_key(sid).sheet1
    # write header if sheet is empty
    if not sh.get_all_values():
        sh.append_row(HEADERS)
    return sh

def vari(lst):
    if len(lst) < 2: return 0.0
    m = sum(lst)/len(lst)
    return round(sum((x-m)**2 for x in lst)/len(lst), 2)

def do_sentiment(text, mood):
    tl = text.lower()
    pos = sum(1 for w in [
        'happy','love','great','awesome','good','wonderful','joy','excited',
        'fantastic','amazing','beautiful','nice','excellent','fun','glad',
        'pleased','cheerful','grateful','brilliant','superb','enjoy'
    ] if w in tl)
    neg = sum(1 for w in [
        'hate','angry','sad','terrible','awful','bad','horrible','frustrated',
        'annoyed','upset','depressed','miserable','worst','disgusting',
        'lonely','stressed','anxious','worried','boring','tired','exhausted'
    ] if w in tl)
    bias = {'happy':2,'excited':2,'calm':1,'neutral':0,
            'lonely':-1,'sad':-2,'frustrated':-2,'angry':-3}.get(mood, 0)
    score   = max(-1.0, min(1.0, (pos - neg + bias) / 5.0))
    emotion = 'Positive' if score > 0.3 else ('Negative' if score < -0.3 else 'Neutral')
    return round(score, 3), emotion

def analyze(data):
    events     = data.get('events', [])
    text       = data.get('text', '')
    mood       = data.get('mood', 'neutral')
    session_id = data.get('session_id', 'unknown')
    sess_start = data.get('session_start', None)
    typed_raw  = data.get('typed_raw', '')   # raw chars including errors before backspace

    # ── Event loop ──────────────────────────────────────────────────────────
    key_holds = {}; keydown_seq = []; inter_delays = []
    pauses = []; bursts = []; current_burst = 0
    last_kd = None; bscount = 0; corr_bursts = []; consec_bs = 0
    PAUSE = 1000

    for ev in events:
        etype = ev.get('type'); key = ev.get('key',''); t = ev.get('time', 0)
        if etype == 'keydown':
            keydown_seq.append((key, t))
            if key == 'Backspace':
                bscount += 1; consec_bs += 1
            else:
                if consec_bs > 1: corr_bursts.append(consec_bs)
                consec_bs = 0
            if last_kd is not None:
                d = t - last_kd; inter_delays.append(d)
                if d > PAUSE:
                    pauses.append(d)
                    if current_burst > 0: bursts.append(current_burst)
                    current_burst = 0
                else:
                    current_burst += 1
            last_kd = t
        elif etype == 'keyup':
            h = ev.get('hold', 0)
            if h > 0: key_holds.setdefault(key, []).append(h)

    if consec_bs > 1: corr_bursts.append(consec_bs)
    if current_burst > 0: bursts.append(current_burst)

    # ── Digraph latencies ────────────────────────────────────────────────────
    dg_lat = {}
    for i in range(len(keydown_seq)-1):
        k1,t1 = keydown_seq[i]; k2,t2 = keydown_seq[i+1]
        if len(k1)==1 and len(k2)==1:
            dg = (k1+k2).lower()
            dg_lat.setdefault(dg,[]).append(t2-t1)
    avg_dg_map = {dg: round(sum(v)/len(v),2) for dg,v in dg_lat.items()}
    cdg = [v for dg,v in avg_dg_map.items() if dg in COMMON_DG]
    avg_dg = round(sum(cdg)/len(cdg),2) if cdg else 0

    # ── Hold stats ───────────────────────────────────────────────────────────
    all_holds = [h for hs in key_holds.values() for h in hs]
    avg_hold  = round(sum(all_holds)/len(all_holds),2) if all_holds else 0
    hold_var  = vari(all_holds)

    # ── Delay stats ──────────────────────────────────────────────────────────
    avg_delay  = round(sum(inter_delays)/len(inter_delays),2) if inter_delays else 0
    delay_var  = vari(inter_delays)
    rhythm     = round((delay_var**0.5)/avg_delay,3) if avg_delay > 0 else 0

    # ── Speed ────────────────────────────────────────────────────────────────
    kd_times   = [t for _,t in keydown_seq]
    dur_ms     = (kd_times[-1]-kd_times[0]) if len(kd_times)>1 else 0
    dur_sec    = round(dur_ms/1000, 2)
    wc         = len(text.split()) if text.strip() else 0
    wpm        = round((wc/dur_sec)*60,1) if dur_sec > 0 else 0
    first_ks   = round(kd_times[0]-sess_start,0) if sess_start and kd_times else 0

    # ── Pauses / bursts ──────────────────────────────────────────────────────
    longest_pause = round(max(pauses),2) if pauses else 0
    avg_pause     = round(sum(pauses)/len(pauses),2) if pauses else 0
    avg_burst_len = round(sum(bursts)/len(bursts),2) if bursts else 0
    max_cb        = max(corr_bursts) if corr_bursts else 0

    # ── Text features ────────────────────────────────────────────────────────
    tc        = len(text)
    bs_ratio  = round(bscount/tc,3) if tc > 0 else 0
    caps_r    = round(sum(1 for c in text if c.isupper())/tc,3) if tc > 0 else 0
    excl      = text.count('!')
    quest     = text.count('?')
    rep       = len(re.findall(r'(.)\1{2,}', text.lower()))
    wds       = re.findall(r'\b\w+\b', text)
    awl       = round(sum(len(w) for w in wds)/len(wds),2) if wds else 0
    sents     = [s.strip() for s in re.split(r'[.!?]+', text) if s.strip()]
    sc        = len(sents)
    asl       = round(wc/sc,2) if sc > 0 else 0
    tl        = text.lower()
    prof      = sum(1 for p in PROFANITY if p in tl)
    hedge     = sum(1 for h in HEDGING if h in tl)

    # ── Error rate (backspaces vs total keystrokes) ──────────────────────────
    total_keys = len([e for e in events if e.get('type')=='keydown'])
    error_rate = round(bscount/total_keys,3) if total_keys > 0 else 0

    score, emotion = do_sentiment(text, mood)

    row = {
        'timestamp': datetime.now().isoformat(), 'session_id': session_id, 'mood': mood,
        'total_chars': tc, 'word_count': wc, 'avg_word_length': awl, 'sentence_count': sc,
        'typing_duration_sec': dur_sec, 'avg_typing_speed_wpm': wpm,
        'first_keystroke_delay_ms': first_ks,
        'avg_key_hold_time_ms': avg_hold, 'hold_time_variance': hold_var,
        'avg_inter_key_delay_ms': avg_delay, 'delay_variance': delay_var,
        'rhythm_irregularity': rhythm, 'avg_digraph_latency_ms': avg_dg,
        'pause_count': len(pauses), 'longest_pause_ms': longest_pause,
        'avg_pause_duration_ms': avg_pause,
        'burst_count': len(bursts), 'avg_burst_length': avg_burst_len,
        'backspace_count': bscount, 'backspace_ratio': bs_ratio,
        'correction_burst_count': len(corr_bursts), 'max_correction_burst': max_cb,
        'caps_ratio': caps_r, 'exclamation_count': excl, 'question_count': quest,
        'repeated_letters_count': rep, 'profanity_count': prof,
        'hedging_word_count': hedge, 'avg_sentence_length': asl,
        'error_rate': error_rate,
        'sentiment_score': score, 'dominant_emotion': emotion,
        'text_sample': text[:200]
    }
    summary = {
        'wpm': wpm, 'avg_hold': avg_hold, 'hold_variance': hold_var,
        'avg_delay': avg_delay, 'delay_variance': delay_var,
        'rhythm_irreg': rhythm, 'digraph_ms': avg_dg,
        'backspaces': bscount, 'corr_bursts': len(corr_bursts),
        'error_rate': error_rate,
        'pauses': len(pauses), 'avg_pause': avg_pause,
        'repeated_letters': rep, 'profanity': prof, 'hedging': hedge,
        'caps_ratio': caps_r, 'sentiment_score': score,
        'dominant_emotion': emotion, 'word_count': wc,
        'duration_sec': dur_sec, 'first_ks_delay': first_ks,
        'avg_word_len': awl, 'bursts': len(bursts)
    }
    return row, summary

@app.route('/api/submit', methods=['POST'])
def submit():
    try:
        data = request.get_json()
        row, summary = analyze(data)
        sheet = get_sheet()
        sheet.append_row([row.get(h,'') for h in HEADERS])
        return jsonify({'status': 'ok', 'summary': summary})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})

# Vercel needs this
app = app
