cask "gluco" do
  version "1.0.9"
  sha256 "bea3df28b7555dc2e056f14a72b840fbbb32ef53922d87540efa8168c92eaaef"

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
