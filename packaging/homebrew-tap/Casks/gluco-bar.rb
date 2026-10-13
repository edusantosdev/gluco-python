cask "gluco-bar" do
  version "1.0.0"
  sha256 "197979037f9a631b6257184b674524d48b9e72d2eb0634d88fe7e58c6400c6f2"

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
