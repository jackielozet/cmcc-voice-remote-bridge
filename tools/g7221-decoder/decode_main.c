/* decode_main.c - 用 libg722_1 把裸 Siren7 比特流解码成 WAV
 * 用法: decode.exe in.raw out.wav [bitrate]
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <g722_1/g722_1.h>

static void put32(FILE *f, unsigned long v)
{
    fputc(v & 0xFF, f); fputc((v >> 8) & 0xFF, f);
    fputc((v >> 16) & 0xFF, f); fputc((v >> 24) & 0xFF, f);
}
static void put16(FILE *f, unsigned v)
{
    fputc(v & 0xFF, f); fputc((v >> 8) & 0xFF, f);
}

int G722_DBG_ERR; int G722_DBG_BITSLEFT;
int main(int argc, char **argv)
{
    FILE *fi, *fo;
    g722_1_decode_state_t *s;
    int bit_rate = 16000;
    int err=0,e4=0,ebl=0,frames=0;
    int frame_bytes, done = 0, total = 0;
    unsigned char buf[512];
    short pcm[2048];
    long data_bytes = 0;
    int n;

    if (argc < 3)
    {
        fprintf(stderr, "usage: %s in.raw out.wav [bitrate]\n", argv[0]);
        return 1;
    }
    if (argc > 3)
        bit_rate = atoi(argv[3]);

    fi = fopen(argv[1], "rb");
    if (!fi) { perror("open input"); return 1; }
    fo = fopen(argv[2], "wb");
    if (!fo) { perror("open output"); return 1; }

    s = g722_1_decode_init(NULL, bit_rate, 16000);
    if (!s) { fprintf(stderr, "decode init failed for rate %d\n", bit_rate); return 1; }

    frame_bytes = (bit_rate / 50) / 8;
    fprintf(stderr, "bit_rate=%d frame_bytes=%d samples/frame=%d\n",
            bit_rate, frame_bytes, MAX_FRAME_SIZE / 2);

    /* WAV 头占位，最后回填 */
    fwrite("RIFF", 1, 4, fo);
    put32(fo, 0);
    fwrite("WAVEfmt ", 1, 8, fo);
    put32(fo, 16);
    put16(fo, 1);          /* PCM */
    put16(fo, 1);          /* mono */
    put32(fo, 16000);      /* sample rate */
    put32(fo, 16000 * 2);  /* byte rate */
    put16(fo, 2);          /* block align */
    put16(fo, 16);         /* bits */
    fwrite("data", 1, 4, fo);
    put32(fo, 0);

    while (1)
    {
        n = (int) fread(buf, 1, frame_bytes, fi);
        if (n < frame_bytes)
            break;
        n = g722_1_decode(s, pcm, buf, frame_bytes);
        if (G722_DBG_ERR) { err++; if (G722_DBG_ERR&4) e4++; if (G722_DBG_BITSLEFT<0) ebl++; }
        if (n > 0)
        {
            fwrite(pcm, sizeof(short), n, fo);
            data_bytes += n * 2;
            total += n;
        }
        done++;
    }

    /* 回填长度 */
    fseek(fo, 4, SEEK_SET);
    put32(fo, (unsigned long) (data_bytes + 36));
    fseek(fo, 40, SEEK_SET);
    put32(fo, (unsigned long) data_bytes);

    fclose(fi);
    fclose(fo);
    fprintf(stderr,"errors: %d/%d (range=%d bitsleft<0=%d)\n",err,done,e4,ebl);
    g722_1_decode_release(s);

    fprintf(stderr, "frames=%d samples=%d duration=%.2fs\n",
            done, total, total / 16000.0);
    return 0;
}
