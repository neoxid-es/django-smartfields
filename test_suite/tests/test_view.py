import os, time, json
from django.test import TestCase, Client
from django.core.urlresolvers import reverse
from django.contrib.auth.models import User
from django.utils.encoding import force_text

from sample_app.models import VideoTestingModel
from sample_app.utils import remove_folder_content


class UploadingTestCase(TestCase):
    fixtures = ['users.json']

    def test_async_uploading(self):
        # this test is a comprehensive one, it tests:
        # * uploading using default FileUploadView,
        # * asynchronous processing,
        # * progress reporting,
        # * ExternalFileProcessor and FFMPEGProcessor,
        # * HTMLTagHandler
        c = Client()
        c.login(username='test_user', password='test_password')
        url = reverse('smartfields:upload', kwargs={
            'app_label': 'sample_app',
            'model': 'videotestingmodel',
            'field_name': 'video_1'
        })
        pk = None
        status = None
        with open("static/videos/badday.wmv", 'rb') as fp:
            response = c.post(url, {'video_1': fp},
                              HTTP_X_REQUESTED_WITH='XMLHttpRequest')
            self.assertEqual(response.status_code, 200)
            status = json.loads(force_text(response.content))
            pk = status['pk']
        self.assertIsNotNone(pk)
        self.assertIsNotNone(status)
        progress = []
        while status['state'] != 'complete':
            if status['state'] == 'processing':
                progress.append(status['progress'])
            time.sleep(1)
            response = c.get(url, {'pk': pk},
                             HTTP_X_REQUESTED_WITH='XMLHttpRequest')
            status = json.loads(force_text(response.content))
        self.assertEqual(
            status['html_tag'],
            '<video id="video_video_1" controls="controls" preload="auto" width="320" '
            'height="240"><source type="video/webm" '
            'src="//example.com/media/sample_app/videotestingmodel/video_1_webm.webm"/>'
            '<source type="video/mp4" '
            'src="//example.com/media/sample_app/videotestingmodel/video_1_mp4.mp4"/>'
            '</video>')
        # make sure progress is within correct bounds [0,1]
        self.assertFalse(list(filter(lambda x: x < 0 or x > 1, progress)))
        # check if we got progress reporting from actual processors 
        self.assertTrue(list(filter(lambda x: x != 0 and x != 1, progress)))
        # make sure it is an increasing progress
        self.assertEqual(progress, sorted(progress))

        # uploading and processing complete, let's verify it's correctness
        instance = VideoTestingModel.objects.get(pk=pk)
        self.assertEqual(instance.video_1.url,
                        "/media/sample_app/videotestingmodel/video_1.wmv")
        self.assertEqual(instance.video_1_mp4.url,
                        "/media/sample_app/videotestingmodel/video_1_mp4.mp4")
        self.assertEqual(instance.video_1_webm.url,
                        "/media/sample_app/videotestingmodel/video_1_webm.webm")
        # make sure files actually exist and they are nonempty
        self.assertTrue(os.path.isfile(instance.video_1.path))
        self.assertTrue(os.path.isfile(instance.video_1_mp4.path))
        self.assertTrue(os.path.isfile(instance.video_1_webm.path))
        self.assertTrue(instance.video_1.size != 0)
        self.assertTrue(instance.video_1_mp4.size != 0)
        self.assertTrue(instance.video_1_webm.size != 0)
        instance.delete()

    def tearDown(self):
        remove_folder_content("media")
        pass
