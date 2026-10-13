cask "gluco-bar" do
  version "1.0.1"
  sha256 "a62d8b186f3dd3d3f4a9bdc1ae1dfdbe8533d8b1505d78ce3a99ef72a6707aff"

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
