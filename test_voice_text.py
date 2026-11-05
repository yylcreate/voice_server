import unittest

from voice_engine import segments
from voice_text import has_speech, mandarin_text, spoken_text


class SpokenTextTests(unittest.TestCase):
    def test_skips_emoji_sequences_without_losing_units_and_punctuation(self):
        self.assertEqual(spoken_text('✅ **工单**２５％ 👩🏽‍🏭！ 保留 -1.2×2。'),
                         '工单25% ！ 保留 -1.2×2。')
        self.assertFalse(has_speech(spoken_text('1️⃣ ™️ 😊！')))
        self.assertEqual(spoken_text('please😊👍check 第1️⃣项 ✅'), 'please check 第项')

    def test_english_words_acronyms_and_chinese_numbers(self):
        self.assertEqual(mandarin_text('工单 A-034425 已完成97.5%。请用 Dashboard、API、B 查看。'),
                         '工单 A 杠零三四四二五，已完成百分之九十七点五。请用 Dashboard、A P I、B 查看。')
        self.assertEqual(mandarin_text('请查API、B、C。'), '请查 A P I、B、C。')
        self.assertEqual(mandarin_text('工单34425，数量319107，偏差-0.5%。'),
                         '工单三四四二五，数量三十一万九千一百零七，偏差负百分之零点五。')
        self.assertEqual(mandarin_text('版本3.0.215，重量1,234.5。'),
                         '版本三点零点二一五，重量一千二百三十四点五。')
        self.assertEqual(mandarin_text('请打开a dashboard。'), '请打开 a dashboard。')

    def test_date_and_time_are_protected_from_stream_split(self):
        text = '加工进度已更新。请核对工单计划时间2026-11-04 12:30:05，再继续查。'
        self.assertTrue(any('2026-11-04' in part for part in segments(text)))
        self.assertTrue(any('12:30:05' in part for part in segments(text)))
        self.assertEqual(mandarin_text('预计2026-11-04 12:30:05。'),
                         '预计二零二六年十一月四日 十二点三十分五秒。')

    def test_closing_quote_and_dotted_acronym_do_not_start_new_run(self):
        text = '生产工单的反馈原话是“正在检查设备。”请在 U.S.A. Dashboard 核对明细。'
        pieces = segments(text)
        self.assertTrue(pieces[0].endswith('。”'))
        self.assertTrue(any('U.S.A.' in piece for piece in pieces))

    def test_ranges_and_english_titles_keep_context(self):
        self.assertEqual(mandarin_text(spoken_text('尺寸10-20毫米，进度80～90%，偏差－0.5。')),
                         '尺寸十到二十毫米，进度百分之八十到百分之九十，偏差负零点五。')
        self.assertEqual(mandarin_text('请找 Dr. Smith 检查。'), '请找 doctor Smith 检查。')
        self.assertFalse(any(part.endswith('Dr.') for part in segments('所有工单已复核，下一步请联系 Dr. Smith 确认进度。')))
