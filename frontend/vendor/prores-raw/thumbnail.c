#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include "libavcodec/avcodec.h"
#include "libavformat/avformat.h"
#include "libavutil/frame.h"
#include "libavutil/intreadwrite.h"
#include "libavutil/raw_color_params.h"
#include "libswscale/swscale.h"
#ifdef __EMSCRIPTEN__
#include <emscripten/heap.h>
size_t thumbnail_heap_bytes(void) { return emscripten_get_heap_size(); }
#endif

static double clamp(double x) { return fmin(1.0, fmax(0.0, x)); }
static double srgb(double x) {
    x = clamp(x);
    return x <= 0.0031308 ? 12.92 * x : 1.055 * pow(x, 1.0 / 2.4) - 0.055;
}

int make_prores_thumbnail(const char *input, const char *output, int display_transform) {
    AVFormatContext *format = NULL;
    AVCodecContext *decoder = NULL, *encoder = NULL;
    AVFrame *frame = av_frame_alloc(), *rgb16 = av_frame_alloc();
    AVFrame *rgb8 = av_frame_alloc(), *jpeg = av_frame_alloc();
    AVPacket *packet = av_packet_alloc(), *encoded = av_packet_alloc();
    struct SwsContext *debayer = NULL, *to_yuv = NULL;
    FILE *destination = NULL;
    int result = -1, received = 0, video = -1;
    const char *phase = "open";
    av_log_set_level(AV_LOG_ERROR);
    if (!frame || !rgb16 || !rgb8 || !jpeg || !packet || !encoded) goto done;
    if (avformat_open_input(&format, input, NULL, NULL) < 0) goto done;
    video = av_find_best_stream(format, AVMEDIA_TYPE_VIDEO, -1, -1, NULL, 0);
    if (video < 0) goto done;
    if (format->streams[video]->codecpar->codec_id != AV_CODEC_ID_PRORES_RAW) { result = -2; goto done; }
    decoder = avcodec_alloc_context3(avcodec_find_decoder(AV_CODEC_ID_PRORES_RAW));
    if (!decoder) goto done;
    avcodec_parameters_to_context(decoder, format->streams[video]->codecpar);
    decoder->thread_count = 1;
    decoder->max_pixels = 40000000;
    decoder->apply_cropping = 0;
    if (avcodec_open2(decoder, decoder->codec, NULL) < 0) goto done;
    for (int count = 0; count < 256 && av_read_frame(format, packet) >= 0; count++) {
        if (packet->stream_index == video) {
            if (packet->size > 32 * 1024 * 1024) { result = -3; goto done; }
            phase = "send_packet";
            int code = avcodec_send_packet(decoder, packet);
            if (code < 0) { result = code; goto done; }
            phase = "receive_frame";
            code = avcodec_receive_frame(decoder, frame);
            if (code == 0) { received = 1; av_packet_unref(packet); break; }
            if (code != AVERROR(EAGAIN)) { result = code; goto done; }
        }
        av_packet_unref(packet);
    }
    if (!received) { result = -4; goto done; }
    if ((frame->crop_left | frame->crop_top) & 1) { result = -7; goto done; }
    if (av_frame_apply_cropping(frame, AV_FRAME_CROP_UNALIGNED) < 0) { result = -8; goto done; }
    if (frame->width <= 0 || frame->height <= 0 || (int64_t)frame->width * frame->height > 40000000) { result = -5; goto done; }
    int width = frame->width >= frame->height ? 120 : (int)lround(120.0 * frame->width / frame->height);
    int height = frame->height >= frame->width ? 120 : (int)lround(120.0 * frame->height / frame->width);
    if (width < 1) width = 1;
    if (height < 1) height = 1;
    rgb16->format = AV_PIX_FMT_RGB48LE; rgb16->width = width; rgb16->height = height;
    rgb8->format = AV_PIX_FMT_RGB24; rgb8->width = width; rgb8->height = height;
    jpeg->format = AV_PIX_FMT_YUVJ444P; jpeg->width = width; jpeg->height = height;
    jpeg->color_range = AVCOL_RANGE_JPEG; jpeg->pts = 0;
    if (av_frame_get_buffer(rgb16, 32) < 0 || av_frame_get_buffer(rgb8, 32) < 0 || av_frame_get_buffer(jpeg, 32) < 0) goto done;
    debayer = sws_getContext(frame->width, frame->height, frame->format, width, height, rgb16->format, SWS_BILINEAR, NULL, NULL, NULL);
    if (!debayer || sws_scale(debayer, (const uint8_t *const *)frame->data, frame->linesize, 0, frame->height, rgb16->data, rgb16->linesize) < 0) goto done;
    AVFrameSideData *side = av_frame_get_side_data(frame, AV_FRAME_DATA_RAW_COLOR_PARAMS);
    const AVRawColorParams *params = side ? (const AVRawColorParams *)side->data : NULL;
    const AVProResRawColorParams *raw = params && params->type == AV_RAW_COLOR_PARAMS_PRORES_RAW ? &params->codec.prores_raw : NULL;
    int has_transform = raw && params->white_level.den && params->black_level.den && raw->wb_red.den && raw->wb_blue.den && raw->gain.den;
    if (has_transform) for (int r = 0; r < 3; r++) for (int c = 0; c < 3; c++) has_transform &= raw->color_matrix[r][c].den != 0;
    double black = has_transform ? av_q2d(params->black_level) : 0.0;
    double white = has_transform ? av_q2d(params->white_level) : 1.0;
    if (white <= black) has_transform = 0;
    if (display_transform && !has_transform) { result = -9; goto done; }
    const double xyz_to_rgb[3][3] = {{3.2404542,-1.5371385,-0.4985314},{-0.9692660,1.8760108,0.0415560},{0.0556434,-0.2040259,1.0572252}};
    // Preview approximation: FFmpeg demosaics first, then we apply camera color at thumbnail resolution.
    for (int y = 0; y < height; y++) for (int x = 0; x < width; x++) {
        const uint8_t *pixel = rgb16->data[0] + y * rgb16->linesize[0] + x * 6;
        double camera[3], rgb[3];
        for (int c = 0; c < 3; c++) camera[c] = AV_RL16(pixel + 2 * c) / 65535.0;
        if (display_transform && has_transform) {
            for (int c = 0; c < 3; c++) camera[c] = fmax(0.0, (camera[c] - black) / (white - black));
            camera[0] *= av_q2d(raw->wb_red); camera[2] *= av_q2d(raw->wb_blue);
            double xyz[3] = {0,0,0};
            for (int r = 0; r < 3; r++) for (int c = 0; c < 3; c++) xyz[r] += av_q2d(raw->color_matrix[r][c]) * camera[c];
            for (int r = 0; r < 3; r++) {
                rgb[r] = 0;
                for (int c = 0; c < 3; c++) rgb[r] += xyz_to_rgb[r][c] * xyz[c];
                rgb[r] *= av_q2d(raw->gain);
            }
        } else for (int c = 0; c < 3; c++) rgb[c] = camera[c];
        for (int c = 0; c < 3; c++) rgb8->data[0][y * rgb8->linesize[0] + x * 3 + c] = (uint8_t)lround(255 * (display_transform ? srgb(rgb[c]) : clamp(rgb[c])));
    }
    to_yuv = sws_getContext(width, height, rgb8->format, width, height, jpeg->format, SWS_BILINEAR, NULL, NULL, NULL);
    if (!to_yuv || sws_scale(to_yuv, (const uint8_t *const *)rgb8->data, rgb8->linesize, 0, height, jpeg->data, jpeg->linesize) < 0) goto done;
    encoder = avcodec_alloc_context3(avcodec_find_encoder(AV_CODEC_ID_MJPEG));
    if (!encoder) goto done;
    encoder->width = width; encoder->height = height; encoder->pix_fmt = jpeg->format;
    encoder->time_base = (AVRational){1,25}; encoder->color_range = AVCOL_RANGE_JPEG;
    encoder->flags |= AV_CODEC_FLAG_QSCALE; encoder->global_quality = 4 * FF_QP2LAMBDA;
    jpeg->quality = encoder->global_quality;
    if (avcodec_open2(encoder, encoder->codec, NULL) < 0 || avcodec_send_frame(encoder, jpeg) < 0 || avcodec_receive_packet(encoder, encoded) < 0) goto done;
    if (encoded->size <= 0 || encoded->size > 512000) { result = -6; goto done; }
    destination = fopen(output, "wb");
    if (!destination || fwrite(encoded->data, 1, encoded->size, destination) != encoded->size) goto done;
    printf("{\"sourceWidth\":%d,\"sourceHeight\":%d,\"width\":%d,\"height\":%d,\"jpegBytes\":%d,\"avioReadBytes\":%lld,\"cameraTransformAvailable\":%d,\"displayTransform\":%d,\"whiteBalanceKelvin\":%u}\n",
        frame->width,frame->height,width,height,encoded->size,(long long)(format->pb ? format->pb->bytes_read : 0),has_transform,display_transform,params?params->wb_cct:0);
    result = 0;
done:
    if (destination) fclose(destination);
    sws_freeContext(debayer); sws_freeContext(to_yuv);
    av_packet_free(&packet); av_packet_free(&encoded);
    av_frame_free(&frame); av_frame_free(&rgb16); av_frame_free(&rgb8); av_frame_free(&jpeg);
    avcodec_free_context(&decoder); avcodec_free_context(&encoder);
    avformat_close_input(&format);
    if (result < 0) fprintf(stderr,"ProRes thumbnail failed in %s: %d\n",phase,result);
    return result;
}

#ifndef __EMSCRIPTEN__
int main(int argc, char **argv) {
    if (argc < 3) return 1;
    return make_prores_thumbnail(argv[1], argv[2], argc > 3) == 0 ? 0 : 1;
}
#endif
