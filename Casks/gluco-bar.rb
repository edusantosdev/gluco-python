cask "gluco-bar" do
  version "1.0.3"
  sha256 "0d5ce250e4d35df83d30a223c54eeb5ae615c505f11d9633823dea97a0fefc59"

  url "https://github.com/edusantosdev/glucobar-python/releases/download/v#{version}/GlucoBar-v#{version}-macos.zip"
  name "GlucoBar"
  desc "macOS menubar glucose display backed by LibreView"
  homepage "https://github.com/edusantosdev/glucobar-python"

  app "GlucoBar.app"

  zap trash: [
    "~/.glucobar",
    "~/Library/LaunchAgents/com.glucobar.frontend.plist",
  ]
end
