cask "gluco-bar" do
  version "1.0.2"
  sha256 "014762e42cf9cbc6ca2e4fd4034add9415a7607ee8128261fc579559a696afe7"

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
