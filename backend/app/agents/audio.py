"""Explicit, opt-in H3 prompt contract; the saved video prompt remains authoritative."""

import re

from pydantic import Field, create_model, model_validator

H3 = "minimax_h3"
VISUAL = "integrated_multimodal_description:"
PERFORMANCE = "Speech performance:"
SOUNDSCAPE = "overall_soundscape:"
MUSIC = "non_diegetic_music:"
TIMING = "Shot timing (seconds):"
SPEECH = re.compile(r"<d>\[([^\[\]<>\n]+)\]\s*([^<>\n]+)</d>")
FRAME_HEADER = re.compile(
    r"\A(?:For the target video, at 0\.00 seconds|How the reference pictures align with the target video).*?\n\n",
    re.DOTALL,
)

H3_INSTRUCTION = (
    "This VIDEO workflow explicitly supports native H3 audio. Its video_prompt must use "
    "these ordered sections: integrated_multimodal_description: [Shot 1] followed by the "
    "visual scene overview. EVERY item is a separate single-shot clip: ALWAYS use [Shot 1] "
    "inside video_prompt, even for later episode shots. Never use [Shot 2] here. "
    "Use a separate paragraph starting Speech performance: inside that "
    "description; then overall_soundscape: and non_diegetic_music:. Write the actual dialogue "
    "and voiceover in the VIDEO prompt, never in image/start/end prompts. Describe a natural, "
    "clear voice and stable speaker ID (S1), (S2) before each verbatim <d>[Chinese] 台词</d> "
    "utterance (use the actual language). Keep original requested words and language. "
    "Match voices to Bible character IDs across adjacent shots. Save speaker ID and voice "
    "description in continuity_state under voice_<character_id> or voice_narrator; reuse "
    "supplied voice entries. For onscreen dialogue, name "
    "the visible speaker and natural synchronized lip movement. For narration explicitly "
    "say 'says in an off-screen voiceover' and keep visible characters' lips closed unless "
    "they also have dialogue. narration_text is only a display copy of offscreen words: "
    "every nonempty narration_text must also appear verbatim inside one <d> tag. All speech "
    "tags belong in Speech performance. Write brief lines feasible within the shot, one "
    "speaker at a time, each line spoken once; very short or purely visual shots may use "
    "Speech performance: No speech. Never invent dialogue or narration for a silent brief. "
    "For an object-only or empty scene, No speech is enough; do not describe mouths or "
    "visible speakers. Sound effects must correspond to actions in THIS clip, not actions "
    "from another shot. "
    "Preserve continuity without repeating the previous shot's sentence. Describe ambient "
    "sounds under overall_soundscape, default music to None unless requested. Keep speech "
    "timing qualitative, not absolute timestamps; action_beats remain visual and will be "
    "appended last. Do not write a Picture reference header; the renderer supplies it. "
    "The English-only visual rule does not translate spoken words."
)


def uses_h3_audio(specifications):
    return any(
        item.get("media_type") == "video"
        and item.get("role") == "prompt"
        and item.get("audio_prompt_format") == H3
        for item in specifications
    )


def audio_section(prompt):
    """Preserve performance, language, voices and soundscape during visual-only repair."""
    if PERFORMANCE not in prompt:
        return None
    return prompt.split(PERFORMANCE, 1)[1].split(TIMING, 1)[0].strip()


def preserve_audio(original, changed):
    section = audio_section(original)
    if section is not None and audio_section(changed) != section:
        raise ValueError("视觉优化不能修改或删除已有的台词、声音表演、环境声和音乐说明")


def reference_header(prompt, capability, duration, *, reference_mode=False):
    """Computed after frame fitting; never prepend to an explicit user override."""
    prompt = FRAME_HEADER.sub("", prompt, count=1)
    if reference_mode:
        # R2V Picture N denotes an appearance reference, not a temporal keyframe.
        # A separate AddGuide may anchor the start without changing that numbering.
        return prompt
    if capability == "FIRST_LAST_TO_VIDEO":
        return (
            "How the reference pictures align with the target video — Picture 1 (from Shot 1) "
            "aligns with the 0.00-second mark of the target video; Picture 2 (from Shot 1) "
            f"aligns with the {duration:.2f}-second mark of the target video.\n\n{prompt}"
        )
    return (
        "For the target video, at 0.00 seconds into the target video, <Picture 1> "
        f"(from [Shot 1]) is fully referenced.\n\n{prompt}"
    )


def validate_h3_prompt(prompt, narration=""):
    markers = [VISUAL, PERFORMANCE, SOUNDSCAPE, MUSIC]
    if any(prompt.count(marker) != 1 for marker in markers):
        raise ValueError("H3 视频提示词必须包含且仅包含一份画面、说话表演、环境声与音乐说明")
    positions = [prompt.index(marker) for marker in markers]
    if positions != sorted(positions) or not prompt.startswith(VISUAL):
        raise ValueError("H3 声画提示词段落顺序不正确")
    if not prompt[len(VISUAL) :].lstrip().startswith("[Shot 1]") or re.search(
        r"\[Shot (?!1\])\d+\]", prompt
    ):
        raise ValueError("每个镜头独立生成，H3 视频提示词内部须使用 [Shot 1]，不能沿用全片镜号")
    for i, marker in enumerate(markers):
        end = positions[i + 1] if i + 1 < len(positions) else len(prompt)
        if not prompt[positions[i] + len(marker) : end].strip():
            raise ValueError("H3 声画说明不能为空，无台词或音乐时明确写 None / No speech")
    performance = prompt[positions[1] + len(PERFORMANCE) : positions[2]]
    lines = SPEECH.findall(performance)
    remainder = SPEECH.sub("", performance)
    outside = prompt[: positions[1]] + prompt[positions[2] :]
    if "<d" in remainder or "</d" in remainder or "<d" in outside or "</d" in outside:
        raise ValueError("台词须在 Speech performance 中使用完整的 <d>[语言] 原文</d>")
    if any(not language.strip() or not words.strip() for language, words in lines):
        raise ValueError("台词语言与原文不能为空")
    if narration.strip() and narration.strip() not in [words.strip() for _, words in lines]:
        raise ValueError("旁白原文必须同时进入视频提示词的台词标签")
    if narration.strip() and "says in an off-screen voiceover" not in performance:
        raise ValueError("H3 旁白段须明确使用 says in an off-screen voiceover")
    return prompt


def audio_output(base, specifications):
    if not uses_h3_audio(specifications):
        return base

    @model_validator(mode="after")
    def validate(self):
        validate_h3_prompt(self.video_prompt, self.narration_text)
        for field in ("image_prompt", "start_frame_prompt", "end_frame_prompt"):
            if "<d>" in getattr(self, field):
                raise ValueError("图片提示词不能包含语音台词标签")
        return self

    return create_model(
        f"NativeAudio{base.__name__}",
        __base__=base,
        __validators__={"validate_native_audio": validate},
        video_prompt=(str, Field(min_length=1, max_length=6000, description=H3_INSTRUCTION)),
        narration_text=(
            str,
            Field(
                default="",
                max_length=2000,
                description="Display copy of offscreen words only; "
                "must also occur verbatim in a video_prompt <d> tag. Empty for onscreen dialogue.",
            ),
        ),
    )
