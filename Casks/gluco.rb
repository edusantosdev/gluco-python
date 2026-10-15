cask "gluco" do
  version "1.0.7"
  sha256 "ddfff0b5ca2433abf2134d0e0040721ea320cfc1f1901c73293847adceb39e53"

  url "https://github.com/edusantosdev/gluco-python/releases/download/v#{version}/Gluco-v#{version}-macos.zip"
  name "Gluco"
  desc "macOS menubar glucose display backed by LibreView"
  homepage "https://github.com/edusantosdev/gluco-python"

  app "Gluco.app"

  zap trash: [
    "~/.glucobar",
    "~/Library/LaunchAgents/com.glucobar.frontend.plist",
  ]
end
