import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import analyzer
import local_ai
import local_analysis
import video_parse_report as report


class LocalInferenceTests(unittest.TestCase):
    def test_login_art_assets_do_not_expose_private_data(self):
        from auth_manager import _public_path, BUBBLE_PUBLIC_ASSETS
        for path in BUBBLE_PUBLIC_ASSETS:
            self.assertTrue(_public_path(path))
            self.assertTrue((Path(__file__).resolve().parents[1] / path.lstrip('/')).is_file())
        for path in ('/api/ai/status','/api/extract/list','/douyin_images/private/01.jpg',
                     '/static/bubble/private.json', '/static/bubble/runtime/private.json',
                     '/static/bubble/runtime/../../secrets_config.py',
                     '/static/bubble/runtime/patch.json', '/static/bubble/bubble-poster.png',
                     '/static/ambient/private.json', '/static/ambient/../../secrets_config.py'):
            self.assertFalse(_public_path(path))

    def test_external_model_address_rejected(self):
        with patch.object(local_ai.cfg, 'BASE_URL', 'https://example.com'):
            with self.assertRaises(local_ai.LocalAIError):
                local_ai._session()

    def test_proxy_environment_is_not_used(self):
        with local_ai._session() as session:
            self.assertFalse(session.trust_env)

    def test_compatibility_client_ignores_old_cloud_model(self):
        with patch.object(local_ai, 'chat', return_value='本地输出') as chat:
            result = local_ai.LocalClient().messages.create(model='old-cloud-model',
                messages=[{'role':'user','content':'素材'}])
        self.assertEqual(result.content[0].text, '本地输出')
        self.assertNotIn('model', chat.call_args.kwargs)

    def test_no_remote_image_fetch(self):
        with self.assertRaises(local_ai.LocalAIError):
            local_ai.LocalClient().create(messages=[{'role':'user','content':[
                {'type':'image','source':{'type':'url','url':'https://example.com/a.png'}}]}])

    def test_transport_error_does_not_retry_whole_analysis(self):
        with patch.object(analyzer, '_call_model', side_effect=local_ai.LocalAIError('offline')) as call:
            with self.assertRaises(local_ai.LocalAIError):
                analyzer.analyze_one('素材')
        self.assertEqual(call.call_count, 1)

    def test_single_vision_failure_still_runs_all_ocr(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = [Path(tmp)/f'{i}.jpg' for i in range(3)]
            for path in files:
                path.write_bytes(b'fake')
            manifest = [{'index':i+1,'path':str(p),'timecode':f'00:00:0{i}.000'} for i,p in enumerate(files)]
            with patch.object(local_analysis,'recognize_text',return_value={'text':'真实字幕'}) as ocr, \
                 patch.object(local_analysis,'describe_image',side_effect=local_ai.LocalAIError('offline')) as vision:
                text=local_analysis.timeline_observations(manifest)
            self.assertEqual(vision.call_count,1)
            self.assertEqual(ocr.call_count,3)
            self.assertEqual(len(report._observation_map(text)),3)
            self.assertFalse(report._has_visual_evidence(text,''))
            self.assertIn('真实字幕',text)

    def test_report_headings_with_and_without_hashes(self):
        for prefix in ('', '## '):
            self.assertEqual(report._report_section(prefix+'【解析结论】\n内容\n\n【下一节】\n其他','解析结论'),'内容')

    def test_light_is_not_action(self):
        group={'representative':{'index':1},'shot_id':1,'start':0,'end':1}
        fields=dict(report._shot_fields(group,{1:['帧001/0','人物微笑','近景','蓝色冷光','字幕','','']},{},[]))
        self.assertEqual(fields['灯光 / 色彩'],'蓝色冷光')
        self.assertEqual(fields['动作 / 表情'],'')

    def test_failed_prose_is_not_visual_evidence(self):
        self.assertFalse(report._has_visual_evidence('本地服务未启动','已保存24帧'))

    def test_classification_preserves_source_and_covers_long_tail(self):
        original = '甲' * 4000 + '真实尾部提示词'
        excerpts = []
        def classify(part, platform, strict):
            excerpts.append(part)
            return json.dumps({'is_prompt':1,'prompt_text':part[-7:],
                               'quality_score':80,'category':'通用'})
        with patch.object(analyzer, '_call_model', side_effect=classify):
            result = analyzer.analyze_one(original)
        self.assertEqual(''.join(excerpts), original)
        self.assertIn('真实尾部提示词', result['prompt_text'])

    def test_invented_prompt_is_not_stored_as_source(self):
        with patch.object(analyzer, '_call_model', return_value=json.dumps(
                {'is_prompt':1,'prompt_text':'编造的提示词','quality_score':90})):
            result = analyzer.analyze_one('真正的原文')
        self.assertEqual(result['prompt_text'],'')
        self.assertEqual(result['is_recommended'],0)

    def test_summary_only_emits_selected_source_sentences(self):
        with patch.object(local_analysis,'chat',return_value='{"indices":[2,999,2],"summary":"编造的结论"}'):
            result=local_analysis.summarize_text('第一句\n[00:01] 原文要点\n第三句')
        self.assertIn('[00:01] 原文要点',result)
        self.assertNotIn('编造的结论',result)
        self.assertNotIn('第三句',result)

    def test_simplified_conversion_and_continuous_shot_numbers(self):
        from text_tools import to_simplified
        self.assertEqual(to_simplified('雖然我們都知道人物說出臺詞'),'虽然我们都知道人物说出台词')
        manifest=[{'index':i+1,'shot_id':sid,'shot_start_sec':i,'shot_end_sec':i+1,'time_sec':i+.2}
                  for i,sid in enumerate((1,3,8))]
        self.assertEqual([s['shot_id'] for s in report._group_shots(manifest)],[1,2,3])


if __name__ == '__main__':
    unittest.main()
