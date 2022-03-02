import QtQuick 2.4
import QtQuick.Layouts 1.4

Item {
    property real strength

    Layout.fillWidth: true
    Layout.fillHeight: true

    Rectangle {
        anchors.centerIn: parent
        width: parent.width/3*2
        radius: width
        height: getLength(sessionData.mycroftgui, strength)
        color: "#40DBB0"
        opacity: getOpacity(sessionData.mycroftgui)

        Behavior on height {
            PropertyAnimation {
                property: "height"
                duration: 50
                easing.type: Easing.InOutQuad
            }
        }
    }
}

