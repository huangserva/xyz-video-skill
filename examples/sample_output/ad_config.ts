export type AdShot = {
  id: number;
  duration: number;
  subtitle: string;
  transition: string;
  imageSrc: string;
  voiceSrc: string;
};

export type AdConfig = {
  meta: Record<string, unknown>;
  variants: Record<string, { width: number; height: number; ratio: string }>;
  shots: AdShot[];
  bgm: Record<string, unknown>;
  branding: Record<string, unknown>;
};

export const adConfig: AdConfig = {
  "meta": {
    "product": "灵感咖啡机",
    "platform": "douyin",
    "duration": 30
  },
  "variants": {
    "vertical": {
      "width": 1080,
      "height": 1920,
      "ratio": "9:16"
    },
    "square": {
      "width": 1080,
      "height": 1080,
      "ratio": "1:1"
    },
    "horizontal": {
      "width": 1920,
      "height": 1080,
      "ratio": "16:9"
    }
  },
  "shots": [
    {
      "id": 1,
      "duration": 4,
      "subtitle": "第1镜：突出灵感咖啡机的痛点场景",
      "transition": "cut",
      "imageSrc": "/private/tmp/xyz-video-skill-mvp-test2/brand/images/shot_001_brand.png",
      "voiceSrc": "/private/tmp/xyz-video-skill-mvp-test2/assets/voiceovers/shot_001.wav"
    },
    {
      "id": 2,
      "duration": 4,
      "subtitle": "第2镜：突出灵感咖啡机的痛点场景",
      "transition": "fade",
      "imageSrc": "/private/tmp/xyz-video-skill-mvp-test2/brand/images/shot_002_brand.png",
      "voiceSrc": "/private/tmp/xyz-video-skill-mvp-test2/assets/voiceovers/shot_002.wav"
    },
    {
      "id": 3,
      "duration": 4,
      "subtitle": "第3镜：突出灵感咖啡机的价值场景",
      "transition": "whip",
      "imageSrc": "/private/tmp/xyz-video-skill-mvp-test2/brand/images/shot_003_brand.png",
      "voiceSrc": "/private/tmp/xyz-video-skill-mvp-test2/assets/voiceovers/shot_003.wav"
    },
    {
      "id": 4,
      "duration": 4,
      "subtitle": "第4镜：突出灵感咖啡机的价值场景",
      "transition": "zoom",
      "imageSrc": "/private/tmp/xyz-video-skill-mvp-test2/brand/images/shot_004_brand.png",
      "voiceSrc": "/private/tmp/xyz-video-skill-mvp-test2/assets/voiceovers/shot_004.wav"
    },
    {
      "id": 5,
      "duration": 4,
      "subtitle": "第5镜：突出灵感咖啡机的价值场景",
      "transition": "cut",
      "imageSrc": "/private/tmp/xyz-video-skill-mvp-test2/brand/images/shot_005_brand.png",
      "voiceSrc": "/private/tmp/xyz-video-skill-mvp-test2/assets/voiceovers/shot_005.wav"
    },
    {
      "id": 6,
      "duration": 4,
      "subtitle": "第6镜：突出灵感咖啡机的价值场景",
      "transition": "fade",
      "imageSrc": "/private/tmp/xyz-video-skill-mvp-test2/brand/images/shot_006_brand.png",
      "voiceSrc": "/private/tmp/xyz-video-skill-mvp-test2/assets/voiceovers/shot_006.wav"
    },
    {
      "id": 7,
      "duration": 3,
      "subtitle": "第7镜：突出灵感咖啡机的价值场景",
      "transition": "whip",
      "imageSrc": "/private/tmp/xyz-video-skill-mvp-test2/brand/images/shot_007_brand.png",
      "voiceSrc": "/private/tmp/xyz-video-skill-mvp-test2/assets/voiceovers/shot_007.wav"
    },
    {
      "id": 8,
      "duration": 3,
      "subtitle": "立即体验 灵感咖啡机，现在行动",
      "transition": "zoom",
      "imageSrc": "/private/tmp/xyz-video-skill-mvp-test2/brand/images/shot_008_brand.png",
      "voiceSrc": "/private/tmp/xyz-video-skill-mvp-test2/assets/voiceovers/shot_008.wav"
    }
  ],
  "bgm": {
    "path": "/private/tmp/xyz-video-skill-mvp-test2/assets/bgm/bgm.wav",
    "provider": "local_silence",
    "style": "温暖钢琴 + 轻弦乐，逐步推进",
    "duration": 30
  },
  "branding": {
    "generated_at": "20260318_210139",
    "brand_color": "#FF6A00",
    "logo": null,
    "watermark_text": "Brand Protected",
    "intro_frame": "/private/tmp/xyz-video-skill-mvp-test2/brand/intro.png",
    "outro_frame": "/private/tmp/xyz-video-skill-mvp-test2/brand/outro.png"
  }
} as AdConfig;
