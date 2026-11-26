// Standalone pixel and anchor tests. Uses the real CUDA kernels with a synthetic frame.
#include "kernels.cuh"
#include <cstdio>
#include <cstdlib>
#include <vector>

using namespace pv5_yolo;
static void require(bool condition, const char *why) {
  if (!condition) { std::fprintf(stderr, "failed: %s\n", why); std::exit(1); }
}
static void check(cudaError_t result) { require(result == cudaSuccess, cudaGetErrorString(result)); }

int main() {
  unsigned char *source;
  __half *image, *output;
  ResultBuffer *result, *device_result;
  cudaStream_t stream;
  check(cudaStreamCreateWithFlags(&stream,cudaStreamNonBlocking));
  check(cudaMallocManaged(&source,12));
  check(cudaMallocManaged(&image,3*kSide*kSide*sizeof(__half)));
  check(cudaMallocManaged(&output,kRows*kColumns*sizeof(__half)));
  check(cudaHostAlloc(&result,sizeof(ResultBuffer),cudaHostAllocMapped));
  check(cudaHostGetDevicePointer(&device_result,result,0));
  const unsigned char planes[] = {16,235,81,145,128,128};
  for (int i=0;i<6;++i) source[i]=planes[i];
  preprocess<<<1600,256,0,stream>>>(source,2,2,0,true,640,640,image);
  check(cudaStreamSynchronize(stream));
  // Corners read a unique Y parity at x/y edges. Neutral chroma yields grayscale.
  const int corners[] = {0,639,639*640,640*640-1};
  const float expected[] = {0,76/255.f,1,150/255.f};
  for (int c=0;c<3;++c) for (int i=0;i<4;++i)
    require(fabsf(__half2float(image[c*640*640+corners[i]])-expected[i])<.002, "packed Y parity / limited range");
  for (int i=0;i<6;++i) source[i]=0;
  source[0]=255; source[5]=255;
  preprocess<<<1600,256,0,stream>>>(source,2,1,6,false,640,320,image);
  check(cudaStreamSynchronize(stream));
  require(fabsf(__half2float(image[0])-114/255.f)<.001,"letterbox");
  require(__half2float(image[160*640])==1 && __half2float(image[2*640*640+160*640+639])==1,"RGB order");
  require(fabsf(__half2float(image[320*640+320])-.5f)<.01,"bilinear");

  check(cudaMemsetAsync(output,0,kRows*kColumns*sizeof(__half),stream));
  check(cudaStreamSynchronize(stream));
  for (int n=0;n<4;++n) {
    __half *p=output+n*kColumns;
    p[0]=__float2half(120.f); p[1]=__float2half(180.f);
    p[2]=__float2half(20.f); p[3]=__float2half(30.f);
    p[4]=__float2half(.5f); p[5+2]=__float2half(n==2 ? .15f : .75f);
  }
  output[kColumns+5+26]=__float2half(.9f); // unsupported winning class must abstain
  output[3*kColumns+5+7]=__float2half(.75f); // deterministic equal-score tie
  result->count=0;
  decode<<<(kRows+255)/256,256,0,stream>>>(output,device_result);
  check(cudaStreamSynchronize(stream));
  require(result->count==2,"class argmax / score");
  for (int i=0;i<2;++i) {
    const auto r=result->rows[i];
    require((r.anchor==0 && r.class_id==2) || (r.anchor==3 && r.class_id==7),"anchor identity");
    require(r.xyxy[0]==110.f && r.xyxy[3]==195.f && r.confidence==.375f,"XYXY / score");
  }
  check(cudaFreeHost(result)); check(cudaFree(source)); check(cudaFree(image)); check(cudaFree(output));
  check(cudaStreamDestroy(stream));
  std::puts("CUDA packed YUV/RGB, FP16 letterbox, resize and YOLO candidate tests passed");
}
