cask "gluco-bar" do
  version "1.0.4"
  sha256 "810f7186a0a26b431a8c42f1e0fe35cd111960547f951cb83743d294b8d0a6d9"

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
