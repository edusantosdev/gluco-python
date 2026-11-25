cask "gluco" do
  version "1.0.10"
  sha256 "b84d56149161b56c5916e5044484b906abe0cc490fd7416655b5262ea238f8ca"

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
