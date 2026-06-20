import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // 隐藏 dev mode 左下角路由指示器（小 N 标）。Next 16 写法:false 即关闭。
  // 编译 / 运行时错误浮层不受影响,仍然会显示。
  devIndicators: false,
};

export default nextConfig;
