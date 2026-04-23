cask "glucobar" do
  version "0.3.0"
  sha256 "88674649c1b44cadd1e8a3f0486f1c0b32c0f659d51c6f367775793d0726257d"

  url "https://github.com/eduardo/librelinkup-glucose-menubar-python/releases/download/v#{version}/Glucobar-v#{version}-macos.zip"
  name "Glucobar"
  desc "macOS menubar glucose display backed by LibreView"
  homepage "https://github.com/eduardo/librelinkup-glucose-menubar-python"

  app "Glucobar.app"

  zap trash: [
    "~/.glucobar",
    "~/Library/LaunchAgents/com.glucobar.frontend.plist",
  ]
end
